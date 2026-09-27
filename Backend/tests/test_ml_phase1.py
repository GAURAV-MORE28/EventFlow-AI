"""Audit Phase 1: verified model bundles, `node_risk()` / `model_info()`,
persisted predictions, and one online evaluation definition for every predictor."""
from __future__ import annotations

import asyncio
import json
import shutil
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.engine import get_engine
from app.services.prediction_eval import OnlineEvaluator
from app.topology import build_topology

API = "/api/v1"
REPO = Path(__file__).resolve().parents[2]
BUNDLE = REPO / "ML" / "artifacts" / "hx_cascade_v2"
sys.path.insert(0, str(REPO))

torch = pytest.importorskip("torch")
pytest.importorskip("torch_geometric")
from ML.cascade import CascadePredictor  # noqa: E402
from ML.manifest import load_manifest, write_manifest  # noqa: E402

needs_bundle = pytest.mark.skipif(not (BUNDLE / "manifest.json").exists(), reason="v2 bundle not present")


def _state(topo: dict, util: float = 0.6) -> dict[str, dict]:
    return {n["entity_id"]: {"entity_id": n["entity_id"], "entity_type": n["entity_type"],
                             "nominal_capacity": n["nominal_capacity"], "utilisation": util,
                             "forecast_900": util, "forecast_1800": util + 0.1, "forecast_3600": util + 0.2}
            for n in topo["nodes"]}


def _predictor(bundle: Path) -> CascadePredictor:
    return CascadePredictor({"use_gnn": True, "gnn_artifact": str(bundle), "critical_utilisation": 0.9})


def _copy_bundle(tmp_path: Path) -> Path:
    dst = tmp_path / "bundle"
    shutil.copytree(BUNDLE, dst)
    return dst


# --- verified bundles ---------------------------------------------------------------
@needs_bundle
def test_tampered_bundle_is_not_loaded(tmp_path):
    bundle = _copy_bundle(tmp_path)
    norm = json.loads((bundle / "feature_norm.json").read_text())
    norm["capacity_norm_const"] = 1.0
    (bundle / "feature_norm.json").write_text(json.dumps(norm))
    cp = _predictor(bundle)
    assert cp.ready() is False
    assert cp.active_source() == "deterministic"
    assert "sha256" in cp.model_info()["error"]
    topo = build_topology()
    risk = cp.node_risk(_state(topo), topo["edges"], "2026-09-04T15:00:00Z")
    assert risk["source"] == "deterministic" and risk["nodes"] == {} and risk["model_version"] is None


def test_missing_bundle_is_not_ready(tmp_path):
    cp = _predictor(tmp_path / "nowhere")
    assert cp.ready() is False and cp.model_info()["model_version"] is None


def test_model_not_requested_is_ready_without_a_bundle():
    cp = CascadePredictor({"use_gnn": False})
    assert cp.ready() is True and cp.active_source() == "deterministic"


# --- node_risk / model_info ---------------------------------------------------------
@needs_bundle
def test_node_risk_reports_every_horizon_ttc_and_identity():
    cp = _predictor(BUNDLE)
    manifest = load_manifest(BUNDLE)
    topo = build_topology()
    risk = cp.node_risk(_state(topo), topo["edges"], "2026-09-04T15:00:00Z")
    assert risk["source"] == "gnn"
    assert risk["model_version"] == f"hx_cascade_v2@{manifest['files']['model.pt']['sha256'][:8]}"
    assert risk["calibrated"] is False            # no calibration.json in the v2 bundle
    assert risk["topology_match"] is True         # trained on this exact map
    relevant = {"gate", "road", "transport_node", "emergency_facility"}
    types = {n["entity_id"]: n["entity_type"] for n in topo["nodes"]}
    assert risk["nodes"] and all(types[e] in relevant for e in risk["nodes"])
    assert len(risk["nodes"]) == sum(1 for t in types.values() if t in relevant)
    for row in risk["nodes"].values():
        for h in (900, 1800, 3600):
            assert 0.0 <= row[f"p_fail_{h}"] <= 1.0
        assert isinstance(row["ttc_sec"], int) and 0 <= row["ttc_sec"] <= 3600
    info = cp.model_info()
    assert info["checkpoint_sha256"] == manifest["files"]["model.pt"]["sha256"]
    assert "ttc" not in info["evaluated_outputs"]


@needs_bundle
def test_node_risk_flags_a_topology_it_was_not_trained_on():
    cp = _predictor(BUNDLE)
    topo = build_topology()
    state = _state(topo)
    first = next(iter(state))
    state[first]["nominal_capacity"] = float(state[first]["nominal_capacity"]) * 2
    risk = cp.node_risk(state, topo["edges"])
    # Phase 6: the OOD guard refuses the map instead of scoring it
    assert risk["topology_match"] is False and risk["source"] == "deterministic" and risk["nodes"] == {}
    assert risk["fallback_reason"].startswith("topology_hash_mismatch")


@needs_bundle
def test_calibration_temperature_is_applied(tmp_path):
    bundle = _copy_bundle(tmp_path)
    manifest = json.loads((bundle / "manifest.json").read_text())
    (bundle / "calibration.json").write_text(json.dumps({"temperature": {"900": 4.0, "1800": 4.0, "3600": 4.0}}))
    write_manifest(bundle, **{k: v for k, v in manifest.items() if k != "files"})
    topo = build_topology()
    raw = _predictor(BUNDLE).node_risk(_state(topo), topo["edges"])
    cal = _predictor(bundle).node_risk(_state(topo), topo["edges"])
    assert cal["calibrated"] is True
    for eid, row in raw["nodes"].items():
        # A temperature > 1 pulls every probability toward 0.5.
        assert abs(cal["nodes"][eid]["p_fail_3600"] - 0.5) <= abs(row["p_fail_3600"] - 0.5) + 1e-4


# --- one evaluation definition ------------------------------------------------------
def test_online_evaluator_scores_alerts_and_crossings():
    ev = OnlineEvaluator(horizon_sec=300)          # 10 cycles of 30s
    over = {"a": False, "b": False, "c": False}
    ev.update(0, 30, over, {"p": {"a", "b"}})      # alerts on a and b
    ev.update(3, 30, {**over, "a": True}, {"p": set()})         # a crosses: confirmed, lead 90s
    ev.update(5, 30, {**over, "a": True, "c": True}, {"p": set()})  # c crosses unpredicted: missed
    ev.update(10, 30, {**over, "a": True, "c": True}, {"p": set()})  # b's window passed: false alarm
    ev.update(11, 30, {**over, "a": True, "c": True}, {"p": {"a"}})  # already over: not an alert
    s = ev.summary("p")
    assert (s["precision"], s["precision_n"]) == (0.5, 2)
    assert (s["recall"], s["recall_n"]) == (0.5, 2)
    assert (s["lead_time_sec"], s["lead_time_n"]) == (90, 1)
    assert s["open_alerts"] == 0


def test_online_evaluator_judges_each_claim_of_an_early_alert():
    """An alert raised 2 cycles more than one horizon before its crossing and
    held until then: 2 wrong claims, 10 right ones, lead capped at the horizon;
    not one false alarm plus a short-lead catch."""
    ev = OnlineEvaluator(horizon_sec=300)           # 10 cycles
    for t in range(12):                             # flagged at cycles 0..11
        ev.update(t, 30, {"a": False}, {"p": {"a"}})
    ev.update(12, 30, {"a": True}, {"p": set()})    # crosses at 12
    s = ev.summary("p")
    assert (s["precision"], s["precision_n"]) == (round(10 / 12, 3), 12)
    assert (s["recall"], s["lead_time_sec"]) == (1.0, 300)


def test_online_evaluator_does_not_reward_flagging_everything():
    ev = OnlineEvaluator(horizon_sec=300)
    ids = [f"e{i}" for i in range(10)]
    for t in range(40):
        over = {e: (e == "e0" and t == 30) for e in ids}   # one crossing in 40 cycles
        ev.update(t, 30, over, {"all": set(ids)})
    s = ev.summary("all")
    assert s["recall"] == 1.0
    assert s["precision"] < 0.1


def test_inactive_predictor_is_not_charged_with_misses():
    ev = OnlineEvaluator(horizon_sec=300)
    ev.update(0, 30, {"a": False}, {"det": set()})
    ev.update(1, 30, {"a": True}, {"det": set()})   # "gnn" not active this run
    assert ev.summary("gnn")["recall_n"] == 0
    assert ev.summary("det")["recall_n"] == 1


# --- live engine: health, metrics, persistence --------------------------------------
@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        engine = get_engine()
        engine.paused = True
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(engine.demo_control("reset", None, None, None, None))
            for _ in range(80):
                loop.run_until_complete(engine.run_cycle())
        finally:
            loop.close()
        yield c


def test_health_reports_the_model_identity(client):
    cascade = client.get(f"{API}/health").json()["modules"]["cascade"]
    engine = get_engine()
    info = engine.registry.cascade.model_info() if hasattr(engine.registry.cascade, "model_info") else {}
    assert cascade["model_version"] == info.get("model_version")
    if info:
        assert cascade["model_ready"] is info["ready"]


def test_metrics_report_published_cascade_and_model_separately(client):
    prediction = client.get(f"{API}/metrics").json()["prediction"]
    for prefix in ("cascade", "gnn"):
        for key in ("precision", "recall", "lead_time_sec"):
            assert f"{prefix}_{key}" in prediction
            assert prediction[f"{prefix}_{key}"]["sample_size"] is not None


def test_predictions_are_persisted(client):
    from app.db import models
    from app.db.base import SessionLocal

    engine = get_engine()
    with SessionLocal() as s:
        cascades = s.query(models.CascadePrediction).count()
        risk_rows = s.query(models.RiskState).count()
        ml_rows = s.query(models.MLNodePrediction).all()
    assert risk_rows > 0
    if engine.store.cascades:
        assert cascades > 0
    if engine.cascade_ml_mode() != "off":
        assert ml_rows
        version = engine.registry.cascade.model_info()["model_version"]
        assert {r.model_version for r in ml_rows} == {version}
        assert {r.gnn_mode for r in ml_rows} == {engine.cascade_ml_mode()}
        assert all(r.p_fail_900 is not None and r.p_fail_3600 is not None for r in ml_rows)


def test_existing_database_gets_the_new_column(tmp_path, monkeypatch):
    from sqlalchemy import create_engine, inspect, text

    from app.db import base

    old = create_engine(f"sqlite:///{(tmp_path / 'old.db').as_posix()}")
    with old.begin() as conn:
        conn.execute(text("CREATE TABLE cascade_prediction (cascade_id INTEGER PRIMARY KEY, root_entity_id TEXT, "
                          "generated_at TEXT, source TEXT, steps TEXT, total_downstream_failures INTEGER)"))
    monkeypatch.setattr(base, "engine", old)
    base._add_missing_columns()
    assert "model_version" in {c["name"] for c in inspect(old).get_columns("cascade_prediction")}
    base._add_missing_columns()   # idempotent
