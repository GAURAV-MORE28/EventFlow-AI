"""Backend correctness: the causal chain the product claims.

EVENT -> DEMAND -> PEOPLE FLOW -> GRAPH STATE -> RISK -> CASCADE -> INTERVENTION
-> NEW STATE -> REST / WebSocket. Each test checks numbers, not status codes.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.catalog import build_properties
from app.config import get_config
from app.main import app
from app.ml_reference.generator import (
    EMERGENCY_MAX, LINE_MAX, ROAD_MAX, STATION_MAX, ZONE_MAX, SyntheticGenerator,
)
from app.ml_reference.risk import RiskScorer
from app.services.attendee import Planner
from app.services.cascade_flow import build_cascades
from app.services.engine import Engine, get_engine
from app.topology import build_topology

API = "/api/v1"
PHYS_MAX = {"road": ROAD_MAX, "transport_node": STATION_MAX, "transport_route": LINE_MAX, "zone": ZONE_MAX,
            "emergency_facility": EMERGENCY_MAX, "venue": 1.0}


def _generator(events: list[dict] | None = None) -> SyntheticGenerator:
    cfg = get_config().raw
    topo = build_topology()
    topo["properties"] = build_properties(topo["nodes"], topo["edges"])
    topo["events"] = events or cfg["events"]
    return SyntheticGenerator(
        {"demand": cfg["demand"], "hospitality": cfg["hospitality"],
         "sim_start_time": cfg["event"]["sim_start_time"], "critical_utilisation": 0.9},
        topo, 42,
    )


def _events(**attendance: int) -> list[dict]:
    evs = [dict(e) for e in get_config().raw["events"]]
    for e in evs:
        if e["event_id"] in attendance:
            e["expected_attendance"] = attendance[e["event_id"]]
    return evs


def _run(engine, cycles: int) -> None:
    loop = asyncio.new_event_loop()
    try:
        for _ in range(cycles):
            loop.run_until_complete(engine.run_cycle())
    finally:
        loop.close()


def _reset(engine) -> None:
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(engine.demo_control("reset", 42, None, None, None))
    finally:
        loop.close()


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        engine = get_engine()
        engine.paused = True
        _reset(engine)
        _run(engine, 120)  # 15:00 — arrival wave building
        yield c


# --- A. event attendance -> demand -> network ------------------------------------------------
def test_attendance_increase_propagates_through_the_network(client):
    engine = get_engine()
    with engine.world_lock:
        baseline = engine.generator.clone()
    r = client.post(f"{API}/events/evt_fanfest", json={"operator_id": "op", "expected_attendance": 15000})
    assert r.status_code == 200 and r.json()["expected_attendance"] == 15000
    _run(engine, 20)
    for _ in range(20):
        baseline.tick(engine.sim_dt)
    live, base = engine.generator.utilisation(), baseline.utilisation()
    assert live["zone_fanpark"] > base["zone_fanpark"] + 0.05            # the venue fills
    transport = [e for e, n in engine.store.nodes.items() if n["entity_type"] == "transport_node"]
    assert max(live[e] - base[e] for e in transport) > 0.05               # riders load the stations
    # ... and the published state (what REST / WS / the map show) carries it.
    assert engine.store.entity_states["zone_fanpark"]["utilisation"] > base["zone_fanpark"] + 0.03
    client.post(f"{API}/events/evt_fanfest", json={"operator_id": "op", "expected_attendance": 5500})


# --- B / R. conservation, no unexplained creation or destruction --------------------------------
def test_people_are_conserved_per_event():
    evs = _events(evt_demo=90000, evt_expo=30000)
    g = _generator(evs)
    attendance = {e["event_id"]: e["expected_attendance"] for e in evs}
    prev = {}
    for _ in range(460):
        g.tick(30)
        for ev, led in g.event_ledger().items():
            assert led["inside"] >= -1e-6 and led["egressed"] >= -1e-6 and led["pending"] >= -1e-3, (ev, led)
            assert abs(led["arrived"] - (led["inside"] + led["egressed"] + led["pending"])) < 1e-6
            assert led["arrived"] <= attendance[ev] + 1e-6
            if ev in prev:  # people never un-arrive or un-leave
                assert led["arrived"] >= prev[ev]["arrived"] - 1e-6
                assert led["egressed"] >= prev[ev]["egressed"] - 1e-6
        prev = g.event_ledger()


def test_gate_queues_balance_inflow_and_service():
    g = _generator()
    for _ in range(140):
        g.tick(30)
    before = g.flow_state()
    g.tick(30)
    after = g.flow_state()
    for e, fv in after.items():
        if not e.startswith("gate_"):
            continue
        dq = fv["queue_people"] - before[e]["queue_people"]
        assert abs(dq - (fv["inflow_per_min"] - fv["outflow_per_min"]) * 0.5) < 1.0, (e, fv, before[e])
        assert fv["queue_people"] >= 0


def test_the_simulation_is_deterministic():
    a, b = _generator(), _generator()
    for _ in range(200):
        a.tick(30)
        b.tick(30)
    assert a.utilisation() == b.utilisation()
    assert a.flow_state() == b.flow_state()


# --- C / D / E. capacity, venue occupancy vs queue ------------------------------------------------
def test_capacity_is_respected_and_overflow_waits_outside():
    g = _generator(_events(evt_demo=90000, evt_expo=30000))
    saw_queue_at_full_venue = False
    for _ in range(360):
        g.tick(30)
        u, fs = g.utilisation(), g.flow_state()
        for e, x in u.items():
            assert x >= 0.0
            cap = PHYS_MAX.get(g.types[e])
            if cap is not None:
                assert x <= cap * 1.011 + 1e-9, (e, x)  # +1% sensor-scale noise
        if u["stadium_main"] >= 0.999 and fs["stadium_main"]["queue_people"] > 100:
            saw_queue_at_full_venue = True
    assert saw_queue_at_full_venue  # 90k tickets, 70k seats: the rest is a queue, not occupancy


def test_closed_gate_has_no_flow_and_no_capacity_gain():
    g = _generator()
    for _ in range(150):
        g.tick(30)
    cap_before = g.capacity("gate_5")
    g.inject("gate_closure", {"entity_id": "gate_5"})
    for _ in range(4):
        g.tick(30)
    fs = g.flow_state()["gate_5"]
    assert g.utilisation()["gate_5"] == 0.0
    assert fs["outflow_per_min"] == 0.0 and fs["queue_people"] == 0.0
    assert g.capacity("gate_5") <= cap_before


# --- F. deterministic severity ----------------------------------------------------------------------
@pytest.mark.parametrize("util,band", [(0.899, "high"), (0.90, "critical"), (0.749, "moderate"), (0.75, "high")])
def test_band_thresholds_are_exact(util, band):
    r = RiskScorer({"critical_utilisation": 0.90, "warning_utilisation": 0.75})
    assert r.fallback({"x": {"utilisation": util, "entity_type": "gate"}})["x"]["risk_band"] == band


def test_severity_does_not_flicker_down_but_escalates_at_once():
    fake = type("E", (), {})()
    fake.config = get_config()
    fake.store = type("S", (), {})()
    fake.store.band_hold = {}
    fake.store.cycle_number, fake.store.no_hold_until = 50, -1
    hold = int(fake.config.raw["thresholds"].get("band_hold_cycles", 3))
    shown = []
    for _ in range(hold):
        fake.store.entity_states = {"x": {"risk_band": "high", "risk_score": 70}}
        Engine._stabilise_bands(fake, {"x": {"risk_band": "critical"}})
        shown.append((fake.store.entity_states["x"]["risk_band"], fake.store.entity_states["x"]["risk_score"]))
    assert shown[:-1] == [("critical", 81)] * (hold - 1)   # held, score consistent with band
    assert shown[-1] == ("high", 70)                        # accepted once it persisted
    fake.store.entity_states = {"x": {"risk_band": "critical", "risk_score": 90}}
    Engine._stabilise_bands(fake, {"x": {"risk_band": "low"}})
    assert fake.store.entity_states["x"]["risk_band"] == "critical"


# --- G / H. cascade: every outbound path, multi-level, nothing invented, no ML needed -------------
def _toy():
    def n(eid, t, u):
        return {"entity_id": eid, "entity_type": t, "display_name": eid, "nominal_capacity": 1000.0,
                "utilisation": u, "risk_score": 90 if u >= 0.9 else 10}
    nodes = {"A": n("A", "gate", 1.2), "B": n("B", "road", 0.8), "C": n("C", "road", 0.85),
             "D": n("D", "road", 0.8), "E": n("E", "road", 0.1), "F": n("F", "road", 0.5)}
    def e(s, d, c=0.5):
        return {"edge_id": f"{s}__{d}", "src_entity_id": s, "dst_entity_id": d, "edge_type": "adjacent_to",
                "transfer_coefficient": c, "travel_time_sec": 60}
    edges = [e("A", "B"), e("A", "C"), e("B", "D", 1.0), e("A", "E", 0.2), e("F", "D")]
    return nodes, edges


def test_cascade_follows_every_outbound_path_and_multiple_levels():
    nodes, edges = _toy()
    lines = lambda t: (0.75, 0.9)
    [c] = build_cascades(nodes, edges, lines, {"max_roots": 5, "max_depth": 4, "max_steps": 8}, "t")
    by = {s["entity_id"]: s for s in c["steps"]}
    assert c["root_entity_id"] == "A"
    assert by["B"]["source_entity_id"] == "A" and by["C"]["source_entity_id"] == "A"   # both branches
    assert by["D"]["source_entity_id"] == "B" and by["D"]["depth"] == 2                 # second level
    assert "E" not in by   # spare capacity absorbs its share: not a failure
    assert "F" not in by   # not downstream of A: never invented
    # The 300 people over A's line are split by edge coefficient (B .5, C .5, E .2),
    # not copied to each branch; E's share is absorbed by its spare capacity.
    moved = sum(s["flow_change_people"] for s in c["steps"] if s["depth"] == 1)
    assert moved == pytest.approx(300 * (0.5 + 0.5) / 1.2)
    edge_by_id = {x["edge_id"]: x for x in edges}
    for s in c["steps"][1:]:
        edge = edge_by_id[s["via_edge_id"]]
        assert (edge["src_entity_id"], edge["dst_entity_id"]) == (s["source_entity_id"], s["entity_id"])
        assert s["confidence"] is None and s["reason"]
    assert c["ml_enhanced"] is False


def test_live_cascades_use_real_edges_and_work_without_ml(client):
    engine = get_engine()
    cascade = engine.registry.cascade
    names = [n for n in ("predict_all", "node_risk") if hasattr(cascade, n)]   # node_risk: what the cycle calls
    orig = {n: getattr(cascade, n) for n in names}

    def broken(*a, **k):
        raise RuntimeError("model unavailable")

    for n in names:
        setattr(cascade, n, broken)
    try:
        _run(engine, 3)
    finally:
        for n, f in orig.items():
            setattr(cascade, n, f)
    store = engine.store
    assert store.cascades, "cascades must not depend on the ML model"
    edges = {e["edge_id"]: e for e in store.edges}
    for c in store.cascades.values():
        assert c["ml_enhanced"] is False
        for s in c["steps"][1:]:
            e = edges[s["via_edge_id"]]
            assert e["dst_entity_id"] == s["entity_id"] and e["src_entity_id"] == s["source_entity_id"]
    body = client.get(f"{API}/cascade/active").json()
    assert {c["root_entity_id"] for c in body["cascades"]} == set(store.cascades)


def test_forecaster_failure_does_not_stop_the_cycle(client):
    engine = get_engine()
    orig = engine.registry.forecaster.predict

    def broken(*a, **k):
        raise RuntimeError("model unavailable")

    engine.registry.forecaster.predict = broken
    try:
        before = engine.store.cycle_number
        _run(engine, 2)
    finally:
        engine.registry.forecaster.predict = orig
    assert engine.store.cycle_number == before + 2
    assert all(f["points"] for f in engine.store.forecasts.values())


# --- I / J. interventions change the simulation and the published state ------------------------------
def test_approved_action_moves_people_and_is_measured_against_do_nothing(client):
    from app.simtime import shift

    engine = get_engine()
    store = engine.store
    iid = "int_hard_gate"
    store.interventions[iid] = {
        "intervention_id": iid, "intervention_type": "gate_redistribution", "status": "proposed",
        "target_entity_ids": ["gate_5", "gate_2"], "triggered_by_entity_id": "gate_5", "title": "t",
        "description": "d", "estimated_relief_pct": 10.0, "estimated_cost_paise": 1, "estimated_delay_sec": 1,
        "feasibility": 0.9, "rank_score": 0.5, "certificate": None, "created_at": store.sim_time,
        "expires_at": shift(store.sim_time, 1200),
        "_action": {"sources": ["gate_5"], "destination": "gate_2", "fraction": 0.4},
    }
    r = client.post(f"{API}/interventions/{iid}/approve", json={"operator_id": "op"})
    assert r.json()["status"] == "executing"
    _run(engine, 6)
    effect = {e["entity_id"]: e for e in store.interventions[iid]["live_effect"]}
    assert effect["gate_5"]["delta"] < 0 < effect["gate_2"]["delta"]
    live = engine.generator.utilisation()
    assert effect["gate_2"]["utilisation"] == pytest.approx(live["gate_2"], abs=1e-4)
    listed = client.get(f"{API}/interventions?status=executing&limit=50").json()["interventions"]
    assert any(i["intervention_id"] == iid and i["live_effect"] for i in listed)


# --- K / L. what-if isolation, explicit apply-to-live ------------------------------------------------
def test_whatif_never_mutates_the_live_city(client):
    from app.services.simulation import SIMULATIONS

    engine = get_engine()
    with engine.world_lock:
        snap = (engine.generator.utilisation(), engine.generator.elapsed_sec(),
                [m["modifier_id"] for m in engine.generator.modifiers()], engine.generator.flow_state())
    res = SIMULATIONS.run_sync(engine, [{"scenario_type": "gate_closure", "params": {"entity_id": "gate_5"}},
                                        {"scenario_type": "attendance_delta", "params": {"delta_pct": 20}}], 1800, "t")
    with engine.world_lock:
        after = (engine.generator.utilisation(), engine.generator.elapsed_sec(),
                 [m["modifier_id"] for m in engine.generator.modifiers()], engine.generator.flow_state())
    assert after == snap
    assert res["scenario"]["peak_utilisation"] <= 1.81
    assert res["baseline"] != res["scenario"]


def test_apply_to_live_is_explicit_and_reversible(client):
    engine = get_engine()
    assert not engine.generator.is_closed("gate_2")
    d = client.post(f"{API}/disruptions", json={"scenario_type": "gate_closure", "params": {"entity_id": "gate_2"}}).json()
    _run(engine, 2)
    assert engine.generator.is_closed("gate_2") and engine.generator.utilisation()["gate_2"] == 0.0
    client.delete(f"{API}/disruptions/{d['disruption_id']}")
    assert not engine.generator.is_closed("gate_2")


# --- M. travel time follows the simulated network state ----------------------------------------------
def test_travel_time_depends_on_projected_congestion(client):
    engine = get_engine()
    store = engine.store
    calm = {e: 0.1 for e in store.nodes}
    busy = {e: (1.3 if store.nodes[e]["entity_type"] in ("zone", "road", "transport_route", "gate") else 0.1)
            for e in store.nodes}
    delay = {e: (900.0 if store.nodes[e]["entity_type"] == "gate" else 0.0) for e in store.nodes}
    projection = {"sim_time": store.sim_time, "samples": [
        {"offset_sec": 0, "util": calm, "delay": {e: 0.0 for e in store.nodes}},
        {"offset_sec": 1800, "util": busy, "delay": delay},
    ]}
    p = Planner(engine, projection)
    now = p.route(p.search("hotel_core_cluster", "stadium_main", 0, 0.0), 0, "x", "stadium_main")
    later = p.route(p.search("hotel_core_cluster", "stadium_main", 1800, 0.0), 1800, "x", "stadium_main")
    assert later["total_duration_sec"] > now["total_duration_sec"] + 900 * 0.9   # the gate queue is waited
    back = p.route(p.search("stadium_main", "hotel_core_cluster", 1800, 0.0), 1800, "x", "hotel_core_cluster")
    assert back["total_duration_sec"] < later["total_duration_sec"]               # no entry queue going home
    assert p.slowdown(1.3) > p.slowdown(0.5) > p.slowdown(0.0) == 1.0


# --- O / P. REST and WebSocket state agree ---------------------------------------------------------------
def test_state_update_payload_matches_rest(client):
    engine = get_engine()
    prev = engine.store.snapshot_states()
    _run(engine, 1)
    changed = engine.store.changed_entities(prev)
    assert changed
    rest = {e["entity_id"]: e for e in client.get(f"{API}/state").json()["entities"]}
    for st in changed:
        r = rest[st["entity_id"]]
        for k in ("utilisation", "risk_band", "risk_score", "current_count", "queue_people", "inflow_per_min"):
            assert r[k] == st[k], (st["entity_id"], k)


def test_every_endpoint_reports_the_same_entity_values(client):
    state = {e["entity_id"]: e for e in client.get(f"{API}/state").json()["entities"]}
    overview = client.get(f"{API}/overview").json()["domains"]
    for rows in overview.values():
        for row in rows:
            s = state[row["entity_id"]]
            assert (row["utilisation"], row["risk_band"], row["current_count"]) == \
                   (s["utilisation"], s["risk_band"], s["current_count"])
            assert row["queue_people"] == s["queue_people"]
    for eid in ("gate_5", "metro_a", "stadium_main"):
        d = client.get(f"{API}/state/{eid}").json()["state"]
        assert (d["utilisation"], d["risk_band"]) == (state[eid]["utilisation"], state[eid]["risk_band"])


# --- N. reset is deterministic ----------------------------------------------------------------------------
def test_reset_reproduces_the_same_run(client):
    engine = get_engine()

    def run():
        _reset(engine)
        _run(engine, 12)
        return engine.store.sim_time, {e: (s["utilisation"], s["risk_band"]) for e, s in engine.store.entity_states.items()}

    first, second = run(), run()
    assert first == second
    assert not engine.store.disruptions and not engine.counterfactuals
    assert all(i["status"] == "proposed" for i in engine.store.interventions.values())


def test_reset_during_a_cycle_leaves_no_stale_state(client, caplog):
    engine = get_engine()
    _run(engine, 3)
    loop = asyncio.new_event_loop()
    try:
        async def race():
            await asyncio.gather(engine.run_cycle(), engine.demo_control("reset", 42, None, None, None))
        with caplog.at_level("ERROR"):
            loop.run_until_complete(race())
            for _ in range(3):
                loop.run_until_complete(engine.run_cycle())
    finally:
        loop.close()
    assert engine.store.cycle_number == 3
    assert not [r for r in caplog.records if "persistence failed" in r.getMessage()]
    # Nothing from the interrupted run survives in the run tables.
    from sqlalchemy import func, select

    from app.db import models
    from app.db.base import SessionLocal
    from app.simtime import parse

    with SessionLocal() as session:
        latest = session.execute(select(func.max(models.EntityState.sim_time))).scalar()
    assert latest is None or latest.replace(tzinfo=None) <= parse(engine.store.sim_time).replace(tzinfo=None)
