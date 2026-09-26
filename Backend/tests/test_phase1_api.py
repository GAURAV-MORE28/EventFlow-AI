"""Phase 1 — API / contract regression over HTTP (TestClient runs the real lifespan)."""
from __future__ import annotations

import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from app import schemas as S
from app.main import app
from app.services.engine import get_engine


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        engine = get_engine()
        c.post("/api/v1/demo/control", json={"action": "pause"})
        time.sleep(0.3)
        loop = asyncio.new_event_loop()
        try:
            for _ in range(60):
                loop.run_until_complete(engine.run_cycle())
        finally:
            loop.close()
        yield c


@pytest.mark.parametrize("scenario", [
    {"scenario_type": "gate_closure", "params": {"entity_id": "gate_99"}},
    {"scenario_type": "gate_closure", "params": {"entity_id": "road_4"}},
    {"scenario_type": "metro_capacity_delta", "params": {"entity_id": "metro_b", "delta_pct": -150}},
    {"scenario_type": "attendance_delta", "params": {}},
])
def test_invalid_scenarios_are_rejected_over_http(client, scenario):
    r = client.post("/api/v1/simulate", json={"scenarios": [scenario], "horizon_sec": 1800})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_SCENARIO"
    S.ErrorEnvelope(**r.json())


def test_whatif_round_trip_is_schema_valid_and_sane(client):
    r = client.post("/api/v1/simulate", json={"scenarios": [{"scenario_type": "weather_rain", "params": {"intensity": "heavy"}}],
                                              "horizon_sec": 1800, "label": "rain"})
    assert r.status_code == 202
    sid = r.json()["simulation_id"]
    body = None
    for _ in range(100):
        body = client.get(f"/api/v1/simulate/{sid}").json()
        if body["status"] != "running":
            break
        time.sleep(0.05)
    assert body["status"] == "complete"
    S.SimulationResult(**body)
    assert not any(k.startswith("_") for k in body), "internal keys must not reach the wire"
    for side in ("baseline", "scenario"):
        assert 0.0 <= body[side]["peak_utilisation"] <= 3.0
    for c in body["candidate_interventions"]:
        assert c["certificate"]["verdict"] in ("STABLE", "CONDITIONAL", "UNSTABLE")


def test_interventions_carry_explicit_effects_and_certified_response(client):
    body = client.get("/api/v1/interventions?status=all&limit=100").json()
    S.InterventionListResponse(**body)
    assert body["interventions"], "premise: proposals exist by cycle 60"
    for i in body["interventions"]:
        assert i["effect_model"] in ("transfer", "deferral", "none")
        if i["intervention_type"] in ("notify_only", "emergency_corridor"):
            assert i["effect_model"] == "none" and i["action_effects"] == []
        else:
            assert i["action_effects"], i["intervention_type"]
        cert = client.get(f"/api/v1/certificates/{i['intervention_id']}").json()
        S.Certificate(**cert)
        assert "response_rate" in cert


def test_twin_and_metrics_are_schema_valid_and_measured(client):
    twin = client.get("/api/v1/twin/fidelity").json()
    S.TwinFidelity(**twin)
    assert twin["ensemble_coverage"] is not None and 0.0 <= twin["ensemble_coverage"] <= 1.0
    metrics = client.get("/api/v1/metrics").json()
    S.MetricsResponse(**metrics)
    assert metrics["twin"]["ensemble_coverage"]["value"] == pytest.approx(round(twin["ensemble_coverage"], 2))
    S.RegretResponse(**client.get("/api/v1/regret").json())


def test_state_units_and_casing(client):
    body = client.get("/api/v1/state").json()
    S.StateResponse(**body)
    for e in body["entities"]:
        assert 0.0 <= e["utilisation"] <= 2.0 and e["current_count"] >= 0
        assert all(k == k.lower() for k in e)
