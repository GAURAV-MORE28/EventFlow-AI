"""ML Phase 4: HX-Cascade v3 in `gnn_mode: annotate` — its calibrated probabilities
on every cascade path (live, on demand, What-If), attributed to the model."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.engine import get_engine

API = "/api/v1"
REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

pytest.importorskip("torch")
pytest.importorskip("torch_geometric")


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        engine = get_engine()
        if engine.cascade_ml_mode() != "annotate":
            pytest.skip("cascade model not loaded in annotate mode")
        engine.paused = True
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(engine.demo_control("reset", None, None, None, None))
            for _ in range(200):   # into the afternoon build-up, when cascades exist
                loop.run_until_complete(engine.run_cycle())
        finally:
            loop.close()
        yield c


def _expected(risk_row: dict, eta_sec: int) -> float:
    for h in (900, 1800, 3600):
        if eta_sec <= h:
            return risk_row[f"p_fail_{h}"]
    return risk_row["p_fail_3600"]


def test_health_reports_v3_annotating(client):
    cascade = client.get(f"{API}/health").json()["modules"]["cascade"]
    assert cascade["gnn_mode"] == "annotate"
    assert cascade["model_version"].startswith("hx_cascade_v3@")


def test_live_cascades_carry_the_models_probabilities(client):
    engine = get_engine()
    nodes = engine.store.cascade_ml["nodes"]
    payload = client.get(f"{API}/cascade/active").json()
    assert payload["cascades"], "expected live cascades by 15:40"
    version = engine.registry.cascade.model_version()
    annotated = 0
    for c in payload["cascades"]:
        assert c["source"] == "deterministic"          # the structure's producer (00 §2.5)
        assert c["ml_enhanced"] and c["confidence_source"] == "gnn"
        assert c["confidence_model_version"] == version
        for s in c["steps"]:
            if s["entity_id"] in nodes:                 # the model covers the scored types
                assert s["confidence"] == pytest.approx(_expected(nodes[s["entity_id"]], s["eta_sec"]))
                annotated += 1
            else:
                assert s["confidence"] is None          # never invented for a type it does not score
    assert annotated


def test_on_demand_cascade_is_annotated(client):
    engine = get_engine()
    roots = set(engine.store.cascades)
    eid = next(e for e in engine.store.cascade_ml["nodes"] if e not in roots)
    c = client.get(f"{API}/cascade/{eid}").json()
    assert c["root_entity_id"] == eid
    assert c["confidence_source"] == "gnn" and c["steps"][0]["confidence"] is not None


def test_what_if_cascade_is_annotated(client):
    r = client.post(f"{API}/simulate", json={"scenarios": [{"scenario_type": "attendance_delta",
                                                            "params": {"delta_pct": 30}}]})
    assert r.status_code == 202, r.text
    body = client.get(f"{API}/simulate/{r.json()['simulation_id']}").json()   # TestClient ran the task
    assert body["status"] == "complete", body
    cascade = body["cascade"]
    assert cascade is not None
    assert cascade["confidence_source"] == "gnn"
    assert any(s["confidence"] is not None for s in cascade["steps"])


def test_no_probabilities_outside_annotate(client, monkeypatch):
    engine = get_engine()
    monkeypatch.setitem(engine.config.raw["cascade"], "gnn_mode", "shadow")
    assert engine.cascade_annotation(engine.store.node_state_for_ml()) == {}
    eid = next(iter(engine.store.cascade_ml["nodes"]))
    from app.services.cascade_flow import cascade_for

    out = cascade_for(eid, engine.store.node_state_for_ml(), engine.store.edges, engine.config.thresholds_for,
                      engine.config.raw["cascade"], engine.store.sim_time, set(),
                      **engine.cascade_annotation(engine.store.node_state_for_ml()))
    assert out["confidence_source"] is None and not out["ml_enhanced"]
    assert all(s["confidence"] is None for s in out["steps"])
