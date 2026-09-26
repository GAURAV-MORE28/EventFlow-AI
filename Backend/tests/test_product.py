"""Product behaviour tests: the city model, its operators' tools and its attendees' tools.

These pin the causal claims the product makes — that disruptions propagate,
that approved actions move visitors, that estimates are physically plausible —
rather than implementation detail.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app import schemas as S
from app.catalog import build_properties
from app.config import get_config
from app.main import app
from app.ml_reference.optimiser import InterventionOptimiser
from app.ml_reference.risk import RiskScorer
from app.services.engine import get_engine
from app.topology import build_topology

API = "/api/v1"


def _generator():
    cfg = get_config().raw
    topo = build_topology()
    topo["properties"] = build_properties(topo["nodes"], topo["edges"])
    topo["events"] = cfg["events"]
    from app.ml_reference.generator import SyntheticGenerator

    return SyntheticGenerator(
        {"demand": cfg["demand"], "hospitality": cfg["hospitality"],
         "sim_start_time": cfg["event"]["sim_start_time"], "critical_utilisation": 0.9},
        topo, 42,
    )


def _run(engine, cycles: int) -> None:
    loop = asyncio.new_event_loop()
    try:
        for _ in range(cycles):
            loop.run_until_complete(engine.run_cycle())
    finally:
        loop.close()


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        engine = get_engine()
        engine.paused = True
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(engine.demo_control("reset", 42, None, None, None))
        finally:
            loop.close()
        _run(engine, 170)  # 15:25 — arrival wave building for the 16:00 kick-off
        yield c


# --- severity ---------------------------------------------------------------------
@pytest.mark.parametrize("util,band", [
    (0.30, "low"), (0.50, "low"), (0.80, "high"), (0.90, "critical"),
    (1.00, "critical"), (1.04, "critical"), (1.20, "critical"),
])
def test_capacity_breach_is_never_reported_as_harmless(util, band):
    r = RiskScorer({"critical_utilisation": 0.90, "warning_utilisation": 0.75})
    out = r.score({"x": {"utilisation": util, "entity_type": "zone"}})["x"]
    assert out["risk_band"] == band


def test_severity_is_monotonic_in_utilisation():
    r = RiskScorer({"critical_utilisation": 0.90, "warning_utilisation": 0.75})
    scores = [r.score({"x": {"utilisation": u / 100, "entity_type": "gate"}})["x"]["risk_score"] for u in range(0, 131, 5)]
    assert scores == sorted(scores)


def test_growth_can_raise_but_not_invent_a_critical_band():
    r = RiskScorer({"critical_utilisation": 0.90, "warning_utilisation": 0.75})
    rising = r.score({"x": {"utilisation": 0.70, "entity_type": "zone", "forecast_1800": 1.2}})["x"]
    assert rising["risk_band"] == "high"


# --- the city model ------------------------------------------------------------------
def test_gate_closure_moves_demand_to_the_other_gates():
    base, closed = _generator(), _generator()
    for g in (base, closed):
        for _ in range(170):
            g.tick(30)
    closed.inject("gate_closure", {"entity_id": "gate_5"})
    for g in (base, closed):
        for _ in range(20):
            g.tick(30)
    b, c = base.utilisation(), closed.utilisation()
    assert c["gate_5"] == 0.0
    others = ["gate_1", "gate_2", "gate_3", "gate_4", "gate_6"]
    assert sum(c[g] for g in others) > sum(b[g] for g in others)


def test_transport_outage_pushes_riders_to_the_substitute_station():
    base, out = _generator(), _generator()
    for g in (base, out):
        for _ in range(170):
            g.tick(30)
    out.inject("transport_outage", {"entity_id": "metro_c"})
    for g in (base, out):
        for _ in range(10):
            g.tick(30)
    assert out.utilisation()["metro_c"] == 0.0
    assert out.utilisation()["metro_d"] > base.utilisation()["metro_d"]


def test_redistribution_moves_people_from_source_to_destination_with_compliance():
    base, act = _generator(), _generator()
    for g in (base, act):
        for _ in range(150):
            g.tick(30)
    act.apply_intervention({"intervention_type": "gate_redistribution", "target_entity_ids": ["gate_5", "gate_4"],
                            "_action": {"sources": ["gate_5"], "destination": "gate_4", "fraction": 0.3}}, compliance=0.8)
    for g in (base, act):
        for _ in range(10):
            g.tick(30)
    b, a = base.utilisation(), act.utilisation()
    assert a["gate_5"] < b["gate_5"]
    assert a["gate_4"] > b["gate_4"]
    assert act.stats()["diverted_people"] > 0


def test_event_delay_reshapes_arrivals():
    base, late = _generator(), _generator()
    delayed = [dict(e) for e in get_config().raw["events"]]
    for e in delayed:
        if e["event_id"] == "evt_demo":
            e["start_time"], e["end_time"] = "2026-09-04T16:30:00Z", "2026-09-04T19:00:00Z"
    late.set_events(delayed)
    for g in (base, late):
        for _ in range(180):
            g.tick(30)
    assert late.event_states()["evt_demo"]["arrived"] < base.event_states()["evt_demo"]["arrived"]
    assert late.utilisation()["stadium_main"] < base.utilisation()["stadium_main"]


def test_clone_is_independent_and_identical():
    g = _generator()
    for _ in range(50):
        g.tick(30)
    c = g.clone()
    for _ in range(20):
        g.tick(30)
        c.tick(30)
    assert g.utilisation() == c.utilisation()
    c.inject("gate_closure", {"entity_id": "gate_3"})
    c.tick(30)
    g.tick(30)
    assert not g.is_closed("gate_3") and c.is_closed("gate_3")


# --- digital twin ------------------------------------------------------------------------
def test_twin_estimates_for_unobserved_entities_track_truth(client):
    engine = get_engine()
    truth = engine.generator.ground_truth()
    unobserved = [e for e, s in engine.store.entity_states.items() if not s["is_observed"]]
    assert unobserved, "some entities should be unobserved (sensor coverage < 100%)"
    for eid, st in engine.store.entity_states.items():
        assert 0.0 <= st["utilisation"] < 2.0
    worst = max(abs(engine.store.entity_states[e]["utilisation"] - truth[e]["utilisation"]) for e in unobserved)
    assert worst < 0.1, f"twin estimate off by {worst:.2f} for an unobserved entity"


# --- intervention lifecycle -------------------------------------------------------------
def test_intervention_lifetime_covers_operator_review_time():
    engine = get_engine()
    icfg = get_config().raw["interventions"]
    ttl = engine.intervention_ttl_sec()
    wall_seconds = ttl / engine.speed_multiplier
    assert wall_seconds >= icfg["min_wall_visible_sec"] or ttl >= icfg["max_ttl_sec"]


def test_ranking_demotes_a_higher_relief_unstable_option():
    opt = InterventionOptimiser({"seed": 42})
    items = [
        {"intervention_id": "a", "estimated_relief_pct": 30.0, "estimated_cost_paise": 100_000,
         "estimated_delay_sec": 300, "certificate": {"verdict": "UNSTABLE"}},
        {"intervention_id": "b", "estimated_relief_pct": 20.0, "estimated_cost_paise": 100_000,
         "estimated_delay_sec": 300, "certificate": {"verdict": "STABLE"}},
    ]
    assert [i["intervention_id"] for i in opt.rank(items)] == ["b", "a"]


def test_candidates_carry_a_simulated_evaluation(client):
    body = client.get(f"{API}/interventions?status=all&limit=100").json()
    evaluated = [i for i in body["interventions"] if i["evaluation"]]
    assert evaluated, "proposals must be simulated before they are queued"
    for i in evaluated:
        ev = i["evaluation"]
        assert ev["root_peak_before"] < 3.0 and ev["root_peak_after"] < 3.0
        assert i["intervention_type"] != "notify_only" or i["estimated_relief_pct"] == 0.0


def test_approval_changes_the_city_and_settles_against_a_counterfactual(client):
    engine = get_engine()
    proposals = [i for i in engine.store.interventions_by_status("proposed", 50) if i["intervention_type"] != "notify_only"]
    if not proposals:
        pytest.skip("no actionable proposal at this point of the run")
    item = proposals[0]
    r = client.post(f"{API}/interventions/{item['intervention_id']}/approve", json={"operator_id": "op_test"})
    assert r.status_code == 200
    assert item["intervention_id"] in engine.counterfactuals
    assert any(m["source"] == "intervention" for m in engine.generator.modifiers())
    _run(engine, 32)
    ledger = client.get(f"{API}/regret").json()["entries"]
    entry = next(e for e in ledger if e["intervention_id"] == item["intervention_id"])
    assert -100 <= entry["realised_relief_pct"] <= 100
    assert engine.store.interventions[item["intervention_id"]]["status"] == "completed"


# --- what-if -------------------------------------------------------------------------------
def _simulate(client, scenarios, horizon=1800):
    r = client.post(f"{API}/simulate", json={"scenarios": scenarios, "horizon_sec": horizon})
    assert r.status_code == 202, r.text
    sid = r.json()["simulation_id"]
    body = client.get(f"{API}/simulate/{sid}").json()
    assert body["status"] == "complete", body
    S.SimulationResult(**body)
    return body


def test_whatif_gate_closure_is_physically_plausible(client):
    body = _simulate(client, [{"scenario_type": "gate_closure", "params": {"entity_id": "gate_5"}}])
    for side in ("baseline", "scenario"):
        assert 0.0 <= body[side]["peak_utilisation"] < 3.0
        assert body[side]["critical_count"] < 30
    changed = {c["entity_id"]: c for c in body["top_changes"]}
    assert "gate_5" in changed and changed["gate_5"]["scenario_peak"] < changed["gate_5"]["baseline_peak"]
    assert body["timeline"] and body["timeline"][0]["offset_sec"] == 0


def test_whatif_attendance_increase_raises_pressure(client):
    body = _simulate(client, [{"scenario_type": "attendance_delta", "params": {"delta_pct": 25}}])
    assert body["scenario"]["avg_utilisation"] >= body["baseline"]["avg_utilisation"]
    assert body["delta"]["metrics"]["late_entries"] >= 0


def test_whatif_event_delay_and_validation(client):
    body = _simulate(client, [{"scenario_type": "event_delay", "params": {"event_id": "evt_demo", "delay_min": 30}}])
    assert body["delta"]["metrics"]["venue_pressure"] <= 0.0
    bad = client.post(f"{API}/simulate", json={"scenarios": [{"scenario_type": "gate_closure", "params": {"entity_id": "gate_99"}}]})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "INVALID_SCENARIO"


# --- accommodation -------------------------------------------------------------------------
def test_hotel_list_and_filters(client):
    body = client.get(f"{API}/accommodation/hotels").json()
    S.HotelListResponse(**body)
    assert body["summary"]["properties"] == 20
    for h in body["hotels"]:
        assert 0 <= h["rooms_available"] <= h["rooms_total"]
        assert h["status"] in ("available", "limited", "saturated")
    cheap = client.get(f"{API}/accommodation/hotels?max_price_paise=400000&accessible_only=true").json()
    assert all(h["price_per_night_paise"] <= 400000 and h["accessible"] for h in cheap["hotels"])
    assert client.get(f"{API}/accommodation/hotels?sort=nope").status_code == 400


def test_recommendation_is_multi_factor_and_explained(client):
    listing = client.get(f"{API}/accommodation/hotels?sort=occupancy").json()["hotels"]
    busiest = listing[0]["property_id"]
    body = client.post(f"{API}/accommodation/recommend",
                       json={"event_id": "evt_demo", "current_property_id": busiest, "segment_id": "price_sensitive"}).json()
    S.StayRecommendationResponse(**body)
    assert body["options"], body
    ids = [o["property"]["property_id"] for o in body["options"]]
    assert busiest not in ids
    assert all(o["property"]["rooms_available"] >= 1 for o in body["options"])
    assert set(body["options"][0]["factors"]) == {"availability", "price", "travel", "transport", "congestion"}
    assert body["current"]["property_id"] == busiest and "recommended instead of" in body["explanation"]
    scores = [o["score"] for o in body["options"]]
    assert scores == sorted(scores, reverse=True)


def test_saturation_lists_alternatives(client):
    body = client.get(f"{API}/accommodation/saturation").json()
    S.SaturationResponse(**body)
    for sat in body["saturated"]:
        assert sat["property"]["status"] == "saturated"
        assert all(a["property"]["property_id"] != sat["property"]["property_id"] for a in sat["alternatives"])


def test_hotel_shortage_reduces_supply():
    g = _generator()
    for _ in range(60):
        g.tick(30)
    before = g.stats()["rooms_available"]
    g.inject("hotel_shortage", {"rooms_offline_pct": 20})
    g.tick(30)
    assert g.stats()["rooms_available"] < before


# --- events --------------------------------------------------------------------------------
def test_event_schedule_and_delay(client):
    body = client.get(f"{API}/events").json()
    S.EventListResponse(**body)
    assert len(body["events"]) >= 3
    fan = next(e for e in body["events"] if e["event_id"] == "evt_fanfest")
    r = client.post(f"{API}/events/evt_fanfest", json={"operator_id": "op_test", "expected_attendance": fan["expected_attendance"] + 500})
    assert r.status_code == 200 and r.json()["expected_attendance"] == fan["expected_attendance"] + 500
    assert client.post(f"{API}/events/evt_nope", json={"operator_id": "op_test", "delay_sec": 600}).status_code == 404
    assert client.post(f"{API}/events/evt_fanfest", json={"operator_id": "op_test"}).status_code == 400
    primary = client.get(f"{API}/event").json()
    assert primary["concurrent_events"], "the fan festival and expo overlap the cup final"


# --- attendee --------------------------------------------------------------------------------
def test_journey_is_personal_timed_and_has_a_return(client):
    body = client.post(f"{API}/attendee/journey", json={
        "attendee_id": "att_test", "segment_id": "time_sensitive",
        "origin_entity_id": "htl_central_budget", "destination_entity_id": "stadium_main", "priority": "least_crowded",
    }).json()
    S.JourneyResponse(**body)
    assert body["recommended_route"]["legs"][0]["from_entity_id"] == "hotel_core_cluster"
    assert body["return_route"] and body["return_route"]["legs"][0]["from_entity_id"] == "stadium_main"
    assert body["departure_options"] and sum(o["recommended"] for o in body["departure_options"]) == 1
    assert body["event"]["event_id"] == "evt_demo"
    other = client.post(f"{API}/attendee/journey", json={
        "attendee_id": "att_test2", "segment_id": "price_sensitive",
        "origin_entity_id": "hotel_airport_cluster", "destination_entity_id": "convention_centre",
    })
    assert other.status_code == 200
    bad = client.post(f"{API}/attendee/journey", json={
        "attendee_id": "x", "segment_id": "group", "origin_entity_id": "nowhere", "destination_entity_id": "stadium_main",
    })
    assert bad.status_code == 404


def test_nudges_reach_affected_attendees_and_answers_feed_compliance(client):
    engine = get_engine()
    j = client.post(f"{API}/attendee/journey", json={
        "attendee_id": "att_nudge", "segment_id": "group",
        "origin_entity_id": "metro_c", "destination_entity_id": "stadium_main", "priority": "fastest",
    }).json()
    gate = next((l["to_entity_id"] for l in j["recommended_route"]["legs"] if l["to_entity_id"].startswith("gate_")), None)
    assert gate
    other = "gate_1" if gate != "gate_1" else "gate_2"
    item = {
        "intervention_id": "int_testnudge", "intervention_type": "gate_redistribution", "status": "proposed",
        "target_entity_ids": [gate, other], "triggered_by_entity_id": gate, "title": "t", "description": "d",
        "estimated_relief_pct": 10.0, "estimated_cost_paise": 25_000, "estimated_delay_sec": 240,
        "feasibility": 0.9, "rank_score": 0.5, "certificate": None, "created_at": engine.store.sim_time,
        "expires_at": engine.store.sim_time.replace("T", "T"), "_action": {"sources": [gate], "destination": other, "fraction": 0.3},
    }
    from app.simtime import shift

    item["expires_at"] = shift(engine.store.sim_time, 1200)
    engine.store.interventions[item["intervention_id"]] = item
    r = client.post(f"{API}/interventions/int_testnudge/approve", json={"operator_id": "op_test"}).json()
    assert r["nudges_issued"] >= 1
    nudges = client.get(f"{API}/attendee/nudges?attendee_id=att_nudge").json()["nudges"]
    assert nudges and nudges[0]["target_entity_id"] == other
    before = engine.current_compliance()
    ok = client.post(f"{API}/attendee/nudges/{nudges[0]['nudge_id']}/respond", json={"accepted": True})
    assert ok.status_code == 200
    assert engine.current_compliance() > before
    assert gate in engine.store.attendees["att_nudge"]["avoid"]


# --- disruptions -------------------------------------------------------------------------------
def test_live_disruption_lifecycle(client):
    engine = get_engine()
    r = client.post(f"{API}/disruptions", json={"scenario_type": "gate_closure", "params": {"entity_id": "gate_2"}})
    assert r.status_code == 201
    did = r.json()["disruption_id"]
    assert engine.generator.is_closed("gate_2")
    assert not engine.nominal.is_closed("gate_2"), "the twin's model must not know an unannounced disruption"
    assert any(d["disruption_id"] == did for d in client.get(f"{API}/disruptions").json()["disruptions"])
    assert client.delete(f"{API}/disruptions/{did}").status_code == 200
    assert not engine.generator.is_closed("gate_2")
    assert client.delete(f"{API}/disruptions/{did}").status_code == 404
    bad = client.post(f"{API}/disruptions", json={"scenario_type": "transport_outage", "params": {}})
    assert bad.status_code == 400


# --- overview / metrics -----------------------------------------------------------------------------
def test_overview_groups_every_domain(client):
    body = client.get(f"{API}/overview").json()
    S.OverviewResponse(**body)
    for domain in ("venues", "transport", "roads", "crowd", "parking", "hospitality", "emergency"):
        assert body["domains"][domain], domain


def test_operations_metrics_are_reported(client):
    body = client.get(f"{API}/metrics").json()
    S.MetricsResponse(**body)
    ops = body["operations"]
    for key in ("capacity_utilisation", "peak_congestion", "unmet_room_requests", "avg_travel_time_sec",
                "visitors_redirected", "attendee_compliance"):
        assert key in ops
    assert 0.0 <= body["twin"]["ensemble_coverage"]["value"] <= 1.0


# --- commander ------------------------------------------------------------------------------------------
@pytest.mark.parametrize("query,expect", [
    ("Which hotels are nearing capacity?", "hotels"),
    ("What happens if Gate 3 closes?", "Simulated gate closure"),
    ("Which transport nodes are under pressure?", "percent"),
    ("When should visitors arrive?", ""),
    ("Which area will become critical next?", ""),
    ("How will delaying the Fan Festival by 30 minutes affect congestion?", "Simulated event delay"),
])
def test_commander_answers_product_questions_grounded(client, query, expect):
    body = client.post(f"{API}/commander/query", json={"query": query, "session_id": "t"}).json()
    S.CommanderResponse(**body)
    assert body["grounding"]["ungrounded_count"] == 0, body["grounding"]
    assert body["tool_calls"]
    assert expect in body["response"], body["response"]


def test_journey_survives_a_station_outage_by_walking_to_the_substitute(client):
    """A hotel whose only access is a closed station still gets a route."""
    r = client.post(f"{API}/disruptions", json={"scenario_type": "transport_outage", "params": {"entity_id": "metro_c"}})
    did = r.json()["disruption_id"]
    try:
        body = client.post(f"{API}/attendee/journey", json={
            "attendee_id": "att_outage", "segment_id": "price_sensitive",
            "origin_entity_id": "htl_central_budget", "destination_entity_id": "stadium_main",
        })
        assert body.status_code == 200, body.text
        stops = [l["to_entity_id"] for l in body.json()["recommended_route"]["legs"]]
        assert "metro_c" not in stops
        rec = client.post(f"{API}/accommodation/recommend", json={"current_property_id": "htl_central_budget"}).json()
        assert rec["current"]["transport_closed"] is True
    finally:
        client.delete(f"{API}/disruptions/{did}")


def test_flow_model_satisfies_the_city_model_interface():
    from app.ml_reference.city_model import CityModel

    g = _generator()
    assert isinstance(g, CityModel)
    assert isinstance(g.clone(), CityModel)
    assert "gate_5" in g.gates_of_venue("stadium_main")
    g.inject("gate_closure", {"entity_id": "gate_5"})
    assert g.closed_entities() == {"gate_5"}


def test_reset_returns_the_initial_city_even_when_paused(client):
    engine = get_engine()
    _run(engine, 20)
    before_reset = dict(engine.store.summary)
    engine.paused = True
    r = client.post(f"{API}/demo/control", json={"action": "reset"})
    assert r.status_code == 200
    state = client.get(f"{API}/state").json()
    assert state["cycle_number"] == 0 and state["sim_time"].endswith("14:00:00Z")
    assert len(state["entities"]) == len(engine.store.nodes), "a reset city must not be blank"
    assert client.get(f"{API}/interventions?status=all").json()["interventions"] == []
    # The summary describes the fresh 14:00 city, not the run that was reset.
    assert state["summary"]["critical_count"] <= before_reset["critical_count"]
    assert engine.paused, "reset keeps the paused/playing choice"
    _run(engine, 170)  # restore the module's arrival-wave state for later tests


def test_cancelled_event_empties_its_venue():
    g = _generator()
    for _ in range(150):  # 15:15 — fan festival live
        g.tick(30)
    inside = g.event_states()["evt_fanfest"]["inside"]
    assert inside > 1000
    events = [dict(e) for e in get_config().raw["events"]]
    for e in events:
        if e["event_id"] == "evt_fanfest":
            e["status"] = "cancelled"
    g.set_events(events)
    for _ in range(90):  # 45 minutes later
        g.tick(30)
    assert g.event_states()["evt_fanfest"]["inside"] < inside * 0.05


def _approve_synthetic(client, engine, iid, itype, targets, action):
    from app.simtime import shift

    engine.store.interventions[iid] = {
        "intervention_id": iid, "intervention_type": itype, "status": "proposed",
        "target_entity_ids": targets, "triggered_by_entity_id": targets[0], "title": "t", "description": "d",
        "estimated_relief_pct": 10.0, "estimated_cost_paise": 25_000, "estimated_delay_sec": 240,
        "feasibility": 0.9, "rank_score": 0.5, "certificate": None, "created_at": engine.store.sim_time,
        "expires_at": shift(engine.store.sim_time, 1200), "_action": action,
    }
    return client.post(f"{API}/interventions/{iid}/approve", json={"operator_id": "op_test"}).json()


def test_declining_a_nudge_lowers_compliance_and_keeps_the_plan(client):
    engine = get_engine()
    client.post(f"{API}/attendee/journey", json={"attendee_id": "att_decline", "segment_id": "group",
                                                  "origin_entity_id": "metro_a", "destination_entity_id": "stadium_main"})
    r = _approve_synthetic(client, engine, "int_testdecline", "reroute_transport", ["metro_a", "metro_e"],
                           {"source": "metro_a", "destination": "metro_e", "fraction": 0.3})
    assert r["nudges_issued"] >= 1
    nudge = client.get(f"{API}/attendee/nudges?attendee_id=att_decline").json()["nudges"][0]
    before = engine.current_compliance()
    body = client.post(f"{API}/attendee/nudges/{nudge['nudge_id']}/respond", json={"accepted": False}).json()
    assert body["status"] == "declined" and "avoid_entity_id" not in body["plan_change"]
    assert engine.current_compliance() < before
    assert not engine.store.attendees["att_decline"]["avoid"]


def test_accepting_a_hotel_transfer_moves_the_attendee(client):
    engine = get_engine()
    client.post(f"{API}/attendee/journey", json={"attendee_id": "att_hotel", "segment_id": "price_sensitive",
                                                  "origin_entity_id": "htl_central_budget", "destination_entity_id": "stadium_main"})
    r = _approve_synthetic(client, engine, "int_testhotel", "accommodation_rebalance",
                           ["hotel_core_cluster", "hotel_airport_cluster"],
                           {"source": "hotel_core_cluster", "destination": "hotel_airport_cluster", "fraction": 0.2})
    assert r["nudges_issued"] >= 1
    nudge = client.get(f"{API}/attendee/nudges?attendee_id=att_hotel").json()["nudges"][0]
    body = client.post(f"{API}/attendee/nudges/{nudge['nudge_id']}/respond", json={"accepted": True}).json()
    new_pid = body["plan_change"]["new_origin_property_id"]
    prop = client.get(f"{API}/accommodation/hotels/{new_pid}").json()
    assert prop["cluster_entity_id"] == "hotel_airport_cluster"
    j = client.post(f"{API}/attendee/journey", json={"attendee_id": "att_hotel", "segment_id": "price_sensitive",
                                                      "origin_entity_id": new_pid, "destination_entity_id": "stadium_main"})
    assert j.status_code == 200
