"""Contract conformance tests.

These check the properties the three teams agreed on, not implementation detail:
ordering guarantees, the error envelope, the degradation fields, and the two
rules that break the demo if violated (cascade step order, certificate verdict
ownership).
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app import schemas as S
from app.main import app
from app.ml_reference.equilibrium import derive_verdict, row_verdict
from app.services.engine import get_engine


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        engine = get_engine()
        # Warm the run past the persistence phase so forecasts and cascades exist.
        engine.paused = True
        loop = asyncio.new_event_loop()
        try:
            for _ in range(24):
                loop.run_until_complete(engine.run_cycle())
        finally:
            loop.close()
        yield c


# --- topology ---------------------------------------------------------------
def test_graph_has_at_least_60_nodes_and_the_demo_chain(client):
    body = client.get("/api/v1/graph").json()
    assert len(body["nodes"]) >= 60

    ids = {n["entity_id"] for n in body["nodes"]}
    for eid in ("metro_b", "gate_3", "road_4", "emergency_north", "gate_5"):
        assert eid in ids

    edges = {(e["src_entity_id"], e["dst_entity_id"]) for e in body["edges"]}
    assert ("metro_b", "gate_3") in edges
    assert ("gate_3", "road_4") in edges
    assert ("road_4", "emergency_north") in edges


def test_segments_are_exactly_the_five_frozen_ids(client):
    body = client.get("/api/v1/graph").json()
    assert {s["segment_id"] for s in body["segments"]} == {
        "price_sensitive", "time_sensitive", "accessibility_constrained", "group", "premium",
    }


# --- state ------------------------------------------------------------------
def test_state_validates_and_reports_load_variance(client):
    body = client.get("/api/v1/state").json()
    S.StateResponse(**body)
    assert body["summary"]["load_variance"] >= 0.0
    assert body["sim_time"].endswith("Z")


def test_unknown_entity_returns_the_error_envelope(client):
    response = client.get("/api/v1/state/metro_z")
    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "ENTITY_NOT_FOUND"
    assert body["error"]["detail"]["entity_id"] == "metro_z"
    S.ErrorEnvelope(**body)


# --- forecast ---------------------------------------------------------------
def test_forecast_horizons_are_exactly_the_three_contract_values(client):
    body = client.get("/api/v1/forecast").json()
    assert body["active_source"] in ("persistence", "tsfm", "local_model")
    for forecast in body["forecasts"]:
        assert [p["horizon_sec"] for p in forecast["points"]] == [900, 1800, 3600]


def test_pressure_timeline_is_sorted_and_has_six_fixed_offsets(client):
    body = client.get("/api/v1/forecast/pressure-timeline").json()
    S.PressureTimelineResponse(**body)
    times = [i["time_to_critical_sec"] for i in body["items"]]
    assert times == sorted(times), "frontend renders in order and must not re-sort"
    for item in body["items"]:
        assert [p["horizon_sec"] for p in item["trajectory"]] == [0, 300, 600, 900, 1200, 1800]


# --- cascade ----------------------------------------------------------------
def test_cascade_steps_are_ordered_by_eta_and_only_root_has_no_edge(client):
    body = client.get("/api/v1/cascade/active").json()
    S.ActiveCascadesResponse(**body)
    assert body["source"] in ("deterministic", "gnn")

    for cascade in body["cascades"]:
        etas = [s["eta_sec"] for s in cascade["steps"]]
        assert etas == sorted(etas), "00 §2.5: steps are ordered by eta_sec ascending"
        assert [s["step_index"] for s in cascade["steps"]] == list(range(len(cascade["steps"])))
        for step in cascade["steps"]:
            if step["step_index"] == 0:
                assert step["via_edge_id"] is None
            else:
                assert step["via_edge_id"] is not None


# --- interventions and certificates -----------------------------------------
def test_interventions_are_ranked_descending_and_unstable_is_not_filtered(client):
    body = client.get("/api/v1/interventions?status=all&limit=50").json()
    S.InterventionListResponse(**body)
    scores = [i["rank_score"] for i in body["interventions"]]
    assert scores == sorted(scores, reverse=True)

    for i in body["interventions"]:
        if i["certificate"]:
            # 00 §2.7: UNSTABLE interventions are returned, never filtered server-side.
            assert i["certificate"]["verdict"] in ("STABLE", "CONDITIONAL", "UNSTABLE")
            assert len(i["certificate"]["compliance_sweep"]) == 3
            assert [r["compliance_rate"] for r in i["certificate"]["compliance_sweep"]] == [0.4, 0.6, 0.9]
            assert len(i["certificate"]["reason"]) <= 140


def test_certificate_lowers_rank_of_a_higher_relief_unstable_option(client):
    """03 §6.3 — the arithmetic that is the demo."""
    body = client.get("/api/v1/interventions?status=all&limit=50").json()
    items = [i for i in body["interventions"] if i["certificate"]]
    unstable = [i for i in items if i["certificate"]["verdict"] == "UNSTABLE"]
    stable = [i for i in items if i["certificate"]["verdict"] == "STABLE"]
    if not unstable or not stable:
        pytest.skip("run did not produce both verdicts")

    for u in unstable:
        for s in stable:
            if u["estimated_relief_pct"] > s["estimated_relief_pct"] and u["triggered_by_entity_id"] == s["triggered_by_entity_id"]:
                assert u["rank_score"] < s["rank_score"], (
                    "a higher-relief UNSTABLE option must rank below a STABLE one"
                )


def test_approving_twice_returns_409(client):
    body = client.get("/api/v1/interventions?status=proposed&limit=1").json()
    if not body["interventions"]:
        pytest.skip("no proposal available")
    iid = body["interventions"][0]["intervention_id"]

    first = client.post(f"/api/v1/interventions/{iid}/approve", json={"operator_id": "op_test"})
    assert first.status_code == 200
    assert first.json()["status"] == "executing"

    second = client.post(f"/api/v1/interventions/{iid}/approve", json={"operator_id": "op_test"})
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "INTERVENTION_ALREADY_RESOLVED"


# --- verdict ownership ------------------------------------------------------
def test_verdict_derivation_lives_in_exactly_one_place():
    """03 §5.3 — the table, verbatim. Nothing else may recompute this."""
    assert row_verdict(1.05, 0.1, 0.2) == "UNSTABLE"      # new critical entity
    assert row_verdict(0.5, 0.3, 0.2) == "UNSTABLE"       # variance got worse
    assert row_verdict(0.93, 0.1, 0.2) == "CONDITIONAL"
    assert row_verdict(0.5, 0.1, 0.2) == "STABLE"

    stable = [{"verdict": "STABLE"}] * 3
    assert derive_verdict(stable, True, False) == "STABLE"
    assert derive_verdict(stable, False, False) == "UNSTABLE"   # non-convergence
    assert derive_verdict(stable, True, True) == "UNSTABLE"     # oscillation
    two_bad = [{"verdict": "STABLE"}, {"verdict": "UNSTABLE"}, {"verdict": "UNSTABLE"}]
    assert derive_verdict(two_bad, True, False) == "UNSTABLE"
    one_bad = [{"verdict": "STABLE"}, {"verdict": "STABLE"}, {"verdict": "UNSTABLE"}]
    assert derive_verdict(one_bad, True, False) == "CONDITIONAL"


# --- commander --------------------------------------------------------------
def test_commander_grounds_every_number_it_emits(client):
    for query in (
        "What is the biggest problem right now?",
        "Why is Metro B becoming critical?",
        "What happens if we do nothing?",
        "Which action gives the largest safety improvement?",
    ):
        body = client.post("/api/v1/commander/query", json={"query": query}).json()
        S.CommanderResponse(**body)
        assert body["grounding"]["ungrounded_count"] == 0, (
            f"ungrounded numbers in answer to {query!r}: {body['grounding']['ungrounded']}"
        )
        assert body["grounding"]["passed"] is True
        assert body["tool_calls"], "every answer must carry its sources"


def test_propose_action_has_no_execution_path(client):
    engine = get_engine()
    result = engine.commander.tools.call("propose_action", {"intervention_id": "int_test"})
    assert result == {"queued": True, "intervention_id": "int_test"}


def test_only_the_eight_contract_tools_are_registered(client):
    engine = get_engine()
    assert sorted(engine.commander.tools.names()) == sorted(
        [
            "get_state", "get_forecast", "get_cascade", "get_interventions",
            "get_certificate", "run_whatif", "summarize_window", "propose_action",
        ]
    )
    with pytest.raises(ValueError):
        engine.commander.tools.register("execute_action", lambda: None)


# --- attendee ---------------------------------------------------------------
def test_journey_always_returns_both_routes(client):
    body = client.post(
        "/api/v1/attendee/journey",
        json={
            "attendee_id": "att_demo_1",
            "segment_id": "price_sensitive",
            "origin_entity_id": "hotel_core_cluster",
            "destination_entity_id": "stadium_main",
        },
    ).json()
    S.JourneyResponse(**body)
    assert body["recommended_route"]["legs"], "recommended route must not be empty"
    assert body["shortest_route"]["legs"], "the shortest route is the point of the view"


# --- metrics ----------------------------------------------------------------
def test_metrics_carry_baselines(client):
    body = client.get("/api/v1/metrics").json()
    S.MetricsResponse(**body)
    assert body["prediction"]["forecast_mae"]["baseline_name"] == "persistence"
    assert body["system"]["commander_ungrounded_rate"]["value"] == 0.0
    assert body["system"]["cycle_latency_ms"]["value"] <= 2000


# --- demo control -----------------------------------------------------------
def test_seeded_reset_is_reproducible(client):
    def run_after_reset() -> list[float]:
        client.post("/api/v1/demo/control", json={"action": "reset", "seed": 42})
        engine = get_engine()
        loop = asyncio.new_event_loop()
        try:
            for _ in range(12):
                loop.run_until_complete(engine.run_cycle())
        finally:
            loop.close()
        return [
            engine.store.entity_states[e]["utilisation"]
            for e in sorted(engine.store.nodes)
        ]

    first = run_after_reset()
    second = run_after_reset()
    assert first == second, "seed 42 must reproduce the identical run (01 §8)"


def test_the_demo_cascade_chain_actually_propagates(client):
    """00 §5 names the chain; this asserts the physics reaches the end of it.

    A cascade that stops at the gate is a cascade the demo cannot show, and the
    failure is silent — the endpoint still returns 200 with a shorter `steps`.
    """
    engine = get_engine()
    engine.paused = True
    loop = asyncio.new_event_loop()
    deepest: dict | None = None
    try:
        loop.run_until_complete(engine.demo_control("reset", 42, None, None, None))
        for _ in range(30):
            loop.run_until_complete(engine.run_cycle())
            cascade = engine.store.cascades.get("metro_b")
            if cascade and (deepest is None or cascade["max_depth"] > deepest["max_depth"]):
                deepest = cascade
    finally:
        loop.close()

    assert deepest is not None, "metro_b never became a cascade root"
    reached = [s["entity_id"] for s in deepest["steps"]]
    for entity in ("metro_b", "gate_3", "road_4", "emergency_north"):
        assert entity in reached, f"demo chain never reached {entity}: got {reached}"
