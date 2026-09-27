"""ML Phase 6: the cascade model's out-of-distribution guard, and one propagator."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import get_config
from app.main import app
from app.services.engine import get_engine
from app.topology import build_topology

API = "/api/v1"
REPO = Path(__file__).resolve().parents[2]
V3 = REPO / "ML" / "artifacts" / "hx_cascade_v3"
sys.path.insert(0, str(REPO))

torch = pytest.importorskip("torch")
pytest.importorskip("torch_geometric")
from ML.cascade import CascadePredictor  # noqa: E402

needs_v3 = pytest.mark.skipif(not (V3 / "manifest.json").exists(), reason="v3 bundle not present")


def _v3(**guard) -> CascadePredictor:
    t = get_config().raw["thresholds"]
    return CascadePredictor({"use_gnn": True, "gnn_artifact": str(V3), "critical_utilisation": t["critical_utilisation"],
                             "thresholds_by_type": t.get("by_type", {}), "ood_guard": guard})


def _state(topo: dict) -> dict[str, dict]:
    return {n["entity_id"]: {"entity_type": n["entity_type"], "nominal_capacity": n["nominal_capacity"],
                             "utilisation": 0.5, "forecast_900": 0.55, "forecast_1800": 0.6, "forecast_3600": 0.65,
                             "is_observed": True, "flow_rate_per_min": 2.0}
            for n in topo["nodes"]}


@needs_v3
def test_v3_scores_the_live_map_and_reports_its_ood_check():
    cp = _v3()
    assert cp.model_info()["ood_guard"] == {"enabled": True, "topology_bound": False, "feature_stats": True}
    topo = build_topology()
    risk = cp.node_risk(_state(topo), topo["edges"], "2026-09-04T15:00:00Z")
    assert risk["source"] == "gnn" and risk["fallback_reason"] is None and risk["nodes"]
    assert risk["ood"]["checked"] and risk["ood"]["scored_entities"] > 0
    assert risk["ood"]["out_of_range_entities"] == 0


@needs_v3
def test_v3_falls_back_on_a_feature_outside_its_training_range():
    """A gate far larger than any on the training maps: log_capacity leaves the range."""
    cp = _v3()
    topo = build_topology()
    state = _state(topo)
    gate = next(e for e, s in state.items() if s["entity_type"] == "gate")
    state[gate]["nominal_capacity"] = 1e12
    risk = cp.node_risk(state, topo["edges"])
    assert risk["source"] == "deterministic" and risk["nodes"] == {}
    assert risk["fallback_reason"].startswith("features_out_of_range") and "log_capacity" in risk["fallback_reason"]
    assert risk["ood"]["out_of_range_entities"] == 1


@needs_v3
def test_entities_already_over_their_line_are_not_checked():
    """The model never scores (and was never trained on) an entity already over its
    line, so a surge there is not out of distribution."""
    cp = _v3()
    topo = build_topology()
    state = _state(topo)
    gate = next(e for e, s in state.items() if s["entity_type"] == "gate")
    state[gate].update(utilisation=2.5, forecast_900=2.5, forecast_1800=2.5, forecast_3600=2.5)
    assert cp.node_risk(state, topo["edges"])["fallback_reason"] is None


@needs_v3
def test_v3_falls_back_when_the_embedding_is_unfamiliar():
    cp = _v3(max_embedding_frac=-1.0)   # every scored entity counts as too far
    topo = build_topology()
    risk = cp.node_risk(_state(topo), topo["edges"])
    assert risk["source"] == "deterministic" and risk["fallback_reason"].startswith("embedding_distance")


@needs_v3
def test_guard_can_be_switched_off():
    cp = _v3(enabled=False, max_embedding_frac=-1.0)
    topo = build_topology()
    risk = cp.node_risk(_state(topo), topo["edges"])
    assert risk["source"] == "gnn" and risk["ood"] == {"checked": False}


# --- the engine records the reason ------------------------------------------------------
@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _cycles(engine, n: int) -> None:
    loop = asyncio.new_event_loop()
    try:
        for _ in range(n):
            loop.run_until_complete(engine.run_cycle())
    finally:
        loop.close()


def test_a_refused_graph_is_recorded_and_publishes_without_confidence(client):
    engine = get_engine()
    engine.paused = True
    cascade = engine.registry.cascade
    if engine.cascade_ml_mode() == "off" or not hasattr(cascade, "ood_max_embedding_frac"):
        pytest.skip("no cascade model with an OOD guard loaded")
    saved = cascade.ood_max_embedding_frac
    cascade.ood_max_embedding_frac = -1.0
    try:
        _cycles(engine, 2)
        health = client.get(f"{API}/health").json()["modules"]["cascade"]
        assert health["fallback_reason"].startswith("embedding_distance")
        assert engine.store.cascade_ml["source"] == "deterministic"
        for c in engine.store.cascades.values():
            assert c["ml_enhanced"] is False and all(s["confidence"] is None for s in c["steps"])
    finally:
        cascade.ood_max_embedding_frac = saved
    _cycles(engine, 1)
    assert client.get(f"{API}/health").json()["modules"]["cascade"]["fallback_reason"] is None


# --- one propagator -------------------------------------------------------------------
def test_reference_cascade_is_the_published_propagator():
    """ml_reference builds its cascades with services/cascade_flow, not a copy."""
    from app.ml_reference.cascade import CascadePredictor as Reference
    from app.services.cascade_flow import cascade_for

    cfg = get_config()
    t = cfg.raw["thresholds"]
    ref = Reference({**cfg.raw["cascade"], "critical_utilisation": t["critical_utilisation"],
                     "warning_utilisation": t.get("warning_utilisation", 0.75), "thresholds_by_type": t.get("by_type", {})})
    topo = build_topology()
    state = _state(topo)
    root = next(e for e, s in state.items() if s["entity_type"] == "gate")
    state[root].update(utilisation=1.2, forecast_900=1.3, forecast_1800=1.3, risk_score=95)
    ours = ref.predict(root, state, topo["edges"], generated_at="2026-09-04T15:00:00Z")
    theirs = cascade_for(root, state, topo["edges"], cfg.thresholds_for, cfg.raw["cascade"], "2026-09-04T15:00:00Z")
    assert ours == theirs and ours["steps"]


def test_ml_cascade_module_builds_no_cascades():
    cp = CascadePredictor({"use_gnn": False})
    topo = build_topology()
    assert cp.predict_all(_state(topo), topo["edges"]) == []
    assert cp.predict("gate_1", _state(topo), topo["edges"])["steps"] == []
    assert not hasattr(cp, "_propagate_deterministic")


# --- twin ensemble coverage (03 §3.6) ----------------------------------------------------
def test_twin_ensemble_coverage_is_in_its_target_range(client):
    """Measured, not floored: the 90% ensemble interval covers truth 85-95% of the time."""
    engine = get_engine()
    engine.paused = True
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(engine.demo_control("reset", 42, None, None, None))
    finally:
        loop.close()
    _cycles(engine, 40)
    twin = client.get(f"{API}/metrics").json()["twin"]["ensemble_coverage"]
    assert 0.85 <= twin["value"] <= 0.95, twin
