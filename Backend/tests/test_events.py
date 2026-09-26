"""Event management and immediate reconciliation.

OPERATOR CHANGES EVENT -> authoritative schedule changes -> the current step is
re-simulated under the new schedule -> risk / cascades re-computed -> REST and
WebSocket publish the new state at once (the clock does not move) -> the next
cycles continue from there.
"""
from __future__ import annotations

import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.cascade_flow import projected
from app.services.engine import get_engine
from app.simtime import parse

API = "/api/v1"
DAY = "2026-09-04"


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


def _baseline(engine):
    with engine.world_lock:
        return engine.generator.clone()


def _tick(world, engine, cycles):
    for _ in range(cycles):
        world.tick(engine.sim_dt)


def _create(client, name, venue, start, end, attendance, **extra):
    r = client.post(f"{API}/events", json={"operator_id": "op", "name": name, "venue_entity_id": venue,
                                          "start_time": start, "end_time": end,
                                          "expected_attendance": attendance, **extra})
    assert r.status_code == 201, r.text
    return r.json()


def _ledger(engine, event_id):
    with engine.world_lock:
        return engine.generator.event_ledger()[event_id]


@pytest.fixture()
def client():
    with TestClient(app) as c:
        engine = get_engine()
        engine.paused = True
        time.sleep(0.5)   # let a background cycle already in flight finish first
        _reset(engine)
        _run(engine, 100)   # 14:50
        yield c


# 1 / 5 / 17 ---------------------------------------------------------------------------------------
def test_create_event_with_exact_datetimes_plans_real_demand(client):
    engine = get_engine()
    base = _baseline(engine)
    ev = _create(client, "Concert Night", "zone_north", f"{DAY}T15:30", f"{DAY}T17:30", 20000,
                 category="concert", description="Open-air concert",
                 arrival_window_start=f"{DAY}T14:45", arrival_window_end=f"{DAY}T15:45",
                 departure_window_start=f"{DAY}T17:30", departure_window_end=f"{DAY}T18:30")
    assert ev["event_id"] == "evt_concert_night"
    assert (ev["start_time"], ev["end_time"]) == (f"{DAY}T15:30:00Z", f"{DAY}T17:30:00Z")   # zone-less = UTC
    assert ev["arrival_window_start"] == f"{DAY}T14:45:00Z" and ev["custom_windows"] is True
    listed = client.get(f"{API}/events").json()["events"]
    assert [e["start_time"] for e in listed] == sorted(e["start_time"] for e in listed)        # timeline order
    assert any(e["event_id"] == "evt_concert_night" for e in listed)
    # The simulator uses exactly these times.
    with engine.world_lock:
        st = engine.generator.event_states()["evt_concert_night"]
    start_min = (parse(ev["start_time"]) - parse(engine.store.sim_start)).total_seconds() / 60
    assert st["start_min"] == pytest.approx(start_min) and st["attendance"] == 20000
    _run(engine, 20)
    _tick(base, engine, 20)
    # window 14:45-15:45 (mid 15:15, sd 15 min): ~12% of 20,000 arrive by 15:00
    assert _ledger(engine, "evt_concert_night")["arrived"] > 1500
    assert engine.generator.utilisation()["zone_north"] > base.utilisation()["zone_north"] + 0.1


def test_datetimes_are_canonical_utc_and_validated(client):
    ev = _create(client, "Offset Talk", "convention_centre", f"{DAY}T21:30:00+05:30", f"{DAY}T22:30:00+05:30", 500)
    assert (ev["start_time"], ev["end_time"]) == (f"{DAY}T16:00:00Z", f"{DAY}T17:00:00Z")
    bad = [
        {"start_time": "tomorrow", "end_time": f"{DAY}T18:00"},
        {"start_time": f"{DAY}T18:00", "end_time": f"{DAY}T17:00"},                 # ends before start
        {"start_time": f"{DAY}T10:00", "end_time": f"{DAY}T11:00"},                 # already over
    ]
    for b in bad:
        r = client.post(f"{API}/events", json={"operator_id": "op", "name": "x", "venue_entity_id": "zone_north",
                                              "expected_attendance": 10, **b})
        assert r.status_code == 400, (b, r.text)
    r = client.post(f"{API}/events", json={"operator_id": "op", "name": "x", "venue_entity_id": "zone_core",
                                          "start_time": f"{DAY}T18:00", "end_time": f"{DAY}T19:00", "expected_attendance": 10})
    assert r.status_code == 400   # unreachable venue: nobody could ever attend
    venues = {v["entity_id"] for v in client.get(f"{API}/events/venues").json()["venues"]}
    assert "stadium_main" in venues and "zone_core" not in venues


# 2 / 8 / 9 ---------------------------------------------------------------------------------------
def test_edit_event_moves_the_demand_curve_later_and_earlier(client):
    engine = get_engine()
    later = _create(client, "Later Show", "zone_south", f"{DAY}T16:00", f"{DAY}T18:00", 15000)
    earlier = _create(client, "Earlier Show", "zone_west", f"{DAY}T17:30", f"{DAY}T19:30", 15000)
    base = _baseline(engine)
    r = client.post(f"{API}/events/{later['event_id']}", json={"operator_id": "op", "start_time": f"{DAY}T18:00",
                                                              "end_time": f"{DAY}T21:00", "name": "Later Show (moved)"})
    assert r.status_code == 200
    moved = r.json()
    assert (moved["start_time"], moved["end_time"], moved["name"]) == (f"{DAY}T18:00:00Z", f"{DAY}T21:00:00Z", "Later Show (moved)")
    client.post(f"{API}/events/{earlier['event_id']}", json={"operator_id": "op", "delay_sec": -3600})
    with engine.world_lock:
        states = engine.generator.event_states()
    assert states[later["event_id"]]["start_min"] == pytest.approx(240) and states[later["event_id"]]["end_min"] == pytest.approx(420)
    assert states[earlier["event_id"]]["start_min"] == pytest.approx(150)
    _run(engine, 30)
    _tick(base, engine, 30)
    with engine.world_lock:
        b = base.event_states()
    assert _ledger(engine, later["event_id"])["arrived"] < b[later["event_id"]]["arrived"]       # later -> fewer so far
    assert _ledger(engine, earlier["event_id"])["arrived"] > b[earlier["event_id"]]["arrived"]   # earlier -> more so far


# 6 / 10 / 13 / 14 ------------------------------------------------------------------------------
def test_attendance_to_zero_stops_demand_and_publishes_at_once(client):
    engine = get_engine()
    ev = _create(client, "Event A", "zone_north", f"{DAY}T15:20", f"{DAY}T17:30", 20000)
    _run(engine, 16)
    arrived_before = _ledger(engine, ev["event_id"])["arrived"]
    assert arrived_before > 3000                                           # demand is visible
    cycle, sim_time = engine.store.cycle_number, engine.store.sim_time
    f_before = client.get(f"{API}/forecast?entity_id=zone_north").json()["forecasts"][0]
    base = _baseline(engine)                                               # the city if nothing changed
    with client.websocket_connect("/ws?client=command_centre") as ws:
        assert ws.receive_json()["event"] == "resync"
        r = client.post(f"{API}/events/{ev['event_id']}", json={"operator_id": "op", "expected_attendance": 0})
        ws.send_json({"action": "ping"})
        msgs = []
        while True:
            m = ws.receive_json()
            if m["event"] == "pong":
                break
            msgs.append(m)
    view = r.json()
    assert view["expected_attendance"] == 0 and view["remaining_demand"] == 0
    # Published at once; the clock did not move; nobody already here was removed.
    assert (engine.store.cycle_number, engine.store.sim_time) == (cycle, sim_time)
    assert _ledger(engine, ev["event_id"])["arrived"] == pytest.approx(arrived_before)
    kinds = [m["event"] for m in msgs]
    assert {"tick", "forecast_update", "cascade_update", "state_reconciled", "event_updated"} <= set(kinds)
    f_after = client.get(f"{API}/forecast?entity_id=zone_north").json()["forecasts"][0]
    p = {x["horizon_sec"]: x["predicted_utilisation"] for x in f_after["points"]}
    q = {x["horizon_sec"]: x["predicted_utilisation"] for x in f_before["points"]}
    assert p[1800] < q[1800] - 0.05                                        # the outlook re-planned now
    rest = {e["entity_id"]: e for e in client.get(f"{API}/state").json()["entities"]}
    for m in msgs:
        if m["event"] == "state_update":
            for e in m["payload"]["entities"]:
                assert (e["utilisation"], e["risk_band"]) == (rest[e["entity_id"]]["utilisation"], rest[e["entity_id"]]["risk_band"])
    # From now on: no new visitors from Event A; the network empties vs the unchanged city.
    _run(engine, 10)
    _tick(base, engine, 10)
    assert _ledger(engine, ev["event_id"])["arrived"] == pytest.approx(arrived_before)
    assert _ledger(engine, ev["event_id"])["inside"] > 0                   # existing attendees remain
    live, ref = engine.generator.utilisation(), base.utilisation()
    access = [e for e in ("metro_b", "metro_a", "bus_hub_north") if e in live]
    assert sum(live[e] for e in access) < sum(ref[e] for e in access) - 0.1
    assert engine.store.entity_states["metro_b"]["utilisation"] < ref["metro_b"]


# 7 / 11 (attendance increase) --------------------------------------------------------------------
def test_attendance_increase_raises_demand_through_the_network(client):
    engine = get_engine()
    ev = _create(client, "Event Up", "zone_south", f"{DAY}T15:40", f"{DAY}T17:40", 10000)
    base = _baseline(engine)
    client.post(f"{API}/events/{ev['event_id']}", json={"operator_id": "op", "expected_attendance": 30000})
    _run(engine, 20)
    _tick(base, engine, 20)
    with engine.world_lock:
        live, b = engine.generator.utilisation(), base.utilisation()
    assert _ledger(engine, ev["event_id"])["arrived"] > base.event_states()[ev["event_id"]]["arrived"] * 1.5
    assert live["zone_south"] > b["zone_south"] + 0.05
    transport = [e for e, n in engine.store.nodes.items() if n["entity_type"] in ("transport_node", "transport_route")]
    assert max(live[e] - b[e] for e in transport) > 0.05
    for step in (5000, 0):   # 30k -> 5k -> 0: each change stops new arrivals beyond what is already here
        client.post(f"{API}/events/{ev['event_id']}", json={"operator_id": "op", "expected_attendance": step})
        a0 = _ledger(engine, ev["event_id"])["arrived"]
        _run(engine, 4)
        assert _ledger(engine, ev["event_id"])["arrived"] <= max(a0, step) + 1e-6


# 12 ----------------------------------------------------------------------------------------------
def test_changing_one_event_leaves_the_others_untouched(client):
    engine = get_engine()
    a = _create(client, "Event A", "zone_north", f"{DAY}T15:40", f"{DAY}T17:40", 10000)
    b = _create(client, "Event B", "zone_south", f"{DAY}T15:40", f"{DAY}T17:40", 20000)
    c = _create(client, "Event C", "zone_west", f"{DAY}T15:40", f"{DAY}T17:40", 30000)
    base = _baseline(engine)
    client.post(f"{API}/events/{b['event_id']}", json={"operator_id": "op", "expected_attendance": 0})
    _run(engine, 20)
    _tick(base, engine, 20)
    with engine.world_lock:
        live, ref = engine.generator.event_states(), base.event_states()
    for e in (a, c):
        assert live[e["event_id"]]["arrived"] == pytest.approx(ref[e["event_id"]]["arrived"], rel=1e-9)
        assert live[e["event_id"]]["attendance"] == ref[e["event_id"]]["attendance"]
    assert live[b["event_id"]]["arrived"] < ref[b["event_id"]]["arrived"]
    listed = {e["event_id"]: e for e in client.get(f"{API}/events").json()["events"]}
    assert (listed[a["event_id"]]["expected_attendance"], listed[c["event_id"]]["expected_attendance"]) == (10000, 30000)


# 3 / 4 / 11 / 20 --------------------------------------------------------------------------------
def test_cancel_stops_demand_and_visitors_leave_normally(client):
    engine = get_engine()
    base = _baseline(engine)
    r = client.post(f"{API}/events/evt_fanfest", json={"operator_id": "op", "status": "cancelled"})
    assert r.json()["status"] == "cancelled" and r.json()["remaining_demand"] == 0
    led0 = _ledger(engine, "evt_fanfest")
    _run(engine, 20)
    _tick(base, engine, 20)
    led = _ledger(engine, "evt_fanfest")
    assert led["arrived"] == pytest.approx(led0["arrived"])        # no new visitors
    assert led["inside"] < led0["inside"] and led["egressed"] > 0  # the ones inside leave
    assert led["arrived"] == pytest.approx(led["inside"] + led["egressed"] + led["pending"])
    assert engine.generator.utilisation()["zone_fanpark"] < base.utilisation()["zone_fanpark"]
    assert any(e["event_id"] == "evt_fanfest" and e["status"] == "cancelled"
               for e in client.get(f"{API}/events").json()["events"])   # stays in the record


def test_delete_removes_from_schedule_but_not_from_the_city(client):
    engine = get_engine()
    ev = _create(client, "Pop Fair", "zone_west", f"{DAY}T15:20", f"{DAY}T17:00", 12000)
    _run(engine, 16)
    led0 = _ledger(engine, ev["event_id"])
    assert led0["arrived"] > 1000
    r = client.delete(f"{API}/events/{ev['event_id']}?operator_id=op")
    assert r.status_code == 200 and r.json()["status"] == "deleted" and r.json()["arrived"] > 1000
    assert ev["event_id"] not in {e["event_id"] for e in client.get(f"{API}/events").json()["events"]}
    assert client.post(f"{API}/events/{ev['event_id']}", json={"operator_id": "op", "expected_attendance": 5}).status_code == 404
    sim = client.post(f"{API}/simulate", json={"scenarios": [{"scenario_type": "event_delay",
                                                              "params": {"event_id": ev["event_id"], "delay_min": 10}}]})
    assert sim.status_code in (400, 404)                           # gone from What-If inputs
    _run(engine, 20)
    led = _ledger(engine, ev["event_id"])
    assert led["arrived"] == pytest.approx(led0["arrived"])       # no new demand
    assert led["inside"] < led0["inside"]                          # people leave, nobody is teleported
    assert led["arrived"] == pytest.approx(led["inside"] + led["egressed"] + led["pending"])


# 15 / 16 ----------------------------------------------------------------------------------------
def test_risk_and_cascades_are_recomputed_from_the_new_state(client):
    engine = get_engine()
    _run(engine, 40)   # 15:10 — stadium approach under pressure
    store = engine.store
    crit_before = sum(1 for s in store.entity_states.values() if s["risk_band"] == "critical")
    cycle = store.cycle_number
    client.post(f"{API}/events/evt_demo", json={"operator_id": "op", "expected_attendance": 0})
    crit_after = sum(1 for s in store.entity_states.values() if s["risk_band"] == "critical")
    assert store.cycle_number == cycle
    assert crit_after < crit_before
    # No stale cascade: every root qualifies under the state now published.
    node_state = store.node_state_for_ml()
    for root in store.cascades:
        crit = engine.config.thresholds_for(node_state[root]["entity_type"])[1]
        assert projected(node_state[root]) >= crit
    assert client.get(f"{API}/cascade/active").json()["cascades"] == [
        {**c, "steps": c["steps"]} for c in client.get(f"{API}/cascade/active").json()["cascades"]]
    _run(engine, 60)
    assert "gate_5" not in store.cascades and "road_5" not in store.cascades


# 18 / 19 ----------------------------------------------------------------------------------------
def test_reset_after_mutations_restores_the_configured_schedule(client):
    engine = get_engine()
    configured = [(e["event_id"], e["start_time"], e["end_time"], e["expected_attendance"])
                  for e in client.get(f"{API}/events").json()["events"]]
    x = _create(client, "Temp", "zone_north", f"{DAY}T16:00", f"{DAY}T17:00", 1000)
    client.post(f"{API}/events/evt_expo", json={"operator_id": "op", "expected_attendance": 1})
    client.delete(f"{API}/events/evt_fanfest")
    _reset(engine)
    after = [(e["event_id"], e["start_time"], e["end_time"], e["expected_attendance"])
             for e in client.get(f"{API}/events").json()["events"]]
    assert after == configured
    with engine.world_lock:
        assert x["event_id"] not in engine.generator.event_states()


def test_whatif_after_an_event_change_is_still_isolated(client):
    from app.services.simulation import SIMULATIONS

    engine = get_engine()
    client.post(f"{API}/events/evt_fanfest", json={"operator_id": "op", "expected_attendance": 15000})
    with engine.world_lock:
        snap = (engine.generator.utilisation(), engine.generator.event_ledger())
    SIMULATIONS.run_sync(engine, [{"scenario_type": "event_cancellation", "params": {"event_id": "evt_fanfest"}}], 1800, "t")
    with engine.world_lock:
        assert (engine.generator.utilisation(), engine.generator.event_ledger()) == snap


def test_total_people_are_conserved_through_every_mutation(client):
    engine = get_engine()
    a = _create(client, "Cons A", "zone_north", f"{DAY}T15:20", f"{DAY}T17:00", 8000)
    _run(engine, 10)
    client.post(f"{API}/events/{a['event_id']}", json={"operator_id": "op", "expected_attendance": 20000})
    _run(engine, 10)
    client.post(f"{API}/events/{a['event_id']}", json={"operator_id": "op", "status": "cancelled"})
    _run(engine, 5)
    client.post(f"{API}/events/{a['event_id']}", json={"operator_id": "op", "status": "scheduled"})
    client.delete(f"{API}/events/{a['event_id']}")
    _run(engine, 10)
    with engine.world_lock:
        ledgers = engine.generator.event_ledger()
    for ev, led in ledgers.items():
        assert led["pending"] >= -1e-6 and led["inside"] >= -1e-6, (ev, led)
        assert led["arrived"] == pytest.approx(led["inside"] + led["egressed"] + led["pending"])
