"""ML Phase 5: per-segment compliance from nudge answers (01 §3.12) and the
forecast-correction model's serving path (ML/forecast_correction.py)."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.compliance import ComplianceEstimator
from app.services.engine import get_engine
from app.topology import SEGMENTS

API = "/api/v1"
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def test_no_answers_is_the_prior():
    est = ComplianceEstimator(SEGMENTS, 0.6, 10)
    assert est.pooled([])["mean"] == 0.6
    assert {s["segment_id"]: s["compliance_base_rate"] for s in est.solver_segments([])} == \
        {s["segment_id"]: s["compliance_base_rate"] for s in SEGMENTS}


def test_pooled_estimate_is_the_previous_global_average():
    """Same number the simulator ran at before per-segment estimates existed."""
    answers = [{"segment_id": "group", "accepted": a} for a in (True, False, False, True, False)]
    assert ComplianceEstimator(SEGMENTS, 0.6, 10).pooled(answers)["mean"] == round((0.6 * 10 + 2) / 15, 4)


def test_answers_move_only_their_own_segment():
    est = ComplianceEstimator(SEGMENTS, 0.6, 10)
    prior = {s["segment_id"]: s["compliance_base_rate"] for s in SEGMENTS}
    answers = [{"segment_id": "price_sensitive", "accepted": False}] * 20 + [{"segment_id": None, "accepted": True}]
    post = est.by_segment(answers)
    assert post["price_sensitive"]["mean"] == round(prior["price_sensitive"] * 10 / 30, 4)
    assert post["price_sensitive"]["answers"] == 20
    for sid, p in post.items():
        if sid != "price_sensitive":
            assert (p["mean"], p["answers"]) == (prior[sid], 0)
    # an unsegmented answer still counts in the pool
    assert est.pooled(answers)["answers"] == 21


def test_posterior_converges_and_interval_narrows():
    est = ComplianceEstimator(SEGMENTS, 0.6, 10)
    few = est.by_segment([{"segment_id": "group", "accepted": i % 5 != 0} for i in range(10)])["group"]
    many = est.by_segment([{"segment_id": "group", "accepted": i % 5 != 0} for i in range(1000)])["group"]
    assert abs(many["mean"] - 0.8) < 0.01
    assert many["upper_90"] - many["lower_90"] < few["upper_90"] - few["lower_90"]
    assert 0.0 <= many["lower_90"] <= many["mean"] <= many["upper_90"] <= 1.0


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def engine(client):
    e = get_engine()
    e.paused = True
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(e.demo_control("reset", 42, None, None, None))
    finally:
        loop.close()
    return e


def test_equilibrium_solver_certifies_with_the_answering_segment_posterior(engine, monkeypatch):
    for _ in range(30):
        engine.record_compliance(False, "group")
    group = next(s for s in engine.solver_segments() if s["segment_id"] == "group")
    prior = next(s for s in SEGMENTS if s["segment_id"] == "group")
    assert group["compliance_base_rate"] < prior["compliance_base_rate"] / 2
    # the published segment definition (00 §2.10, /event) is not rewritten
    assert next(s for s in engine.store.segments if s["segment_id"] == "group") == prior

    seen: list[list[dict]] = []
    real = engine.registry.equilibrium.certify

    def spy(candidate, node_state, edges, segments):
        seen.append(segments)
        return real(candidate, node_state, edges, segments)

    monkeypatch.setattr(engine.registry.equilibrium, "certify", spy)
    loop = asyncio.new_event_loop()
    try:
        for _ in range(240):              # into the pre-kick-off arrival wave
            loop.run_until_complete(engine.run_cycle())
            if seen:
                break
    finally:
        loop.close()
    assert seen, "the engine never certified a candidate"
    used = next(s for s in seen[0] if s["segment_id"] == "group")
    assert used["compliance_base_rate"] == group["compliance_base_rate"]


def test_metrics_report_each_segment_against_its_prior(client, engine):
    ops = client.get(f"{API}/metrics").json()["operations"]
    for s in SEGMENTS:
        m = ops[f"compliance_{s['segment_id']}"]
        assert m["baseline"] == s["compliance_base_rate"] and m["baseline_name"] == "segment_prior"
        assert m["sample_size"] == len([a for a in engine.store.observed_compliance
                                        if a["segment_id"] == s["segment_id"]])
    assert ops["attendee_compliance"]["sample_size"] == len(engine.store.observed_compliance)


def test_reset_forgets_answers(engine):
    engine.record_compliance(True, "premium")
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(engine.demo_control("reset", 42, None, None, None))
    finally:
        loop.close()
    assert engine.store.observed_compliance == []
    assert engine.current_compliance() == float(engine.icfg.get("default_compliance", 0.6))


# --- forecast correction (ML/forecast_correction.py) ------------------------------------------
from ML.forecast_correction import (  # noqa: E402
    FEATURE_NAMES, HORIZONS, QUANTILES, ForecastCorrector, booster_file, features, save_booster,
)


def _prior(now=0.5):
    return {"now": now, "points": {900: now + 0.05, 1800: now + 0.1, 3600: now + 0.2}}


def test_features_match_the_declared_layout():
    row = features([0.4, 0.45, 0.5], 1200.0, 0.9, _prior(), {900: 0.55, 1800: 0.6, 3600: 0.7})
    assert len(row) == len(FEATURE_NAMES)
    named = dict(zip(FEATURE_NAMES, row))
    assert named["last"] == 0.5 and named["gap"] == 0.0 and named["twin_headroom_1800"] == pytest.approx(0.3)
    assert named["d1"] == pytest.approx(0.05) and named["d5"] != named["d5"]   # NaN: not enough history


def _bundle(tmp_path, passes=True, offset=0.1):
    """A tiny bundle whose boosters predict a constant residual `offset`."""
    lgb = pytest.importorskip("lightgbm")
    from ML.manifest import write_manifest

    rng = np.random.default_rng(0)
    X = rng.random((400, len(FEATURE_NAMES)))
    for h in HORIZONS:
        for q in QUANTILES:
            y = np.full(400, offset + (q - 0.5) * 0.1)
            b = lgb.train({"objective": "regression", "verbose": -1, "min_data_in_leaf": 50}, lgb.Dataset(X, y), 5)
            save_booster(b, tmp_path / booster_file(h, q))
    (tmp_path / "features.json").write_text(json.dumps(
        {"feature_names": list(FEATURE_NAMES), "horizons_sec": list(HORIZONS), "quantiles": list(QUANTILES)}))
    (tmp_path / "eval.json").write_text(json.dumps({"gate": {"passes": passes}}))
    write_manifest(tmp_path, model_name="forecast_correction", model_version="fc_test")
    return tmp_path


def test_corrector_refuses_a_bundle_that_did_not_pass_its_gate(tmp_path):
    from ML.forecast_correction import CorrectionUnavailable

    with pytest.raises(CorrectionUnavailable, match="gate"):
        ForecastCorrector(_bundle(tmp_path, passes=False))


def test_corrector_refuses_a_tampered_bundle(tmp_path):
    from ML.forecast_correction import CorrectionUnavailable

    b = _bundle(tmp_path)
    (b / booster_file(1800, 0.5)).write_text("tampered")
    with pytest.raises(CorrectionUnavailable):
        ForecastCorrector(b)


def test_corrector_refuses_a_different_feature_layout(tmp_path):
    from ML.forecast_correction import CorrectionUnavailable
    from ML.manifest import write_manifest

    b = _bundle(tmp_path)
    (b / "features.json").write_text(json.dumps({"feature_names": ["last"], "horizons_sec": list(HORIZONS),
                                                  "quantiles": list(QUANTILES)}))
    write_manifest(b, model_name="forecast_correction", model_version="fc_test")
    with pytest.raises(CorrectionUnavailable, match="layout"):
        ForecastCorrector(b)


def _forecast(artifact):
    from app.ml_reference.forecaster import Forecaster

    fc = Forecaster({"correction_artifact": str(artifact) if artifact else None})
    series = {"gate_x": [0.40 + 0.01 * i for i in range(12)], "zone_y": [0.2]}
    out = fc.predict(series, {"gate_x": 1000.0, "zone_y": 500.0}, list(HORIZONS), "2026-09-04T15:00:00Z",
                     model_prior={"gate_x": _prior(0.51)})
    return fc, out


def test_forecaster_serves_the_correction_as_local_model(tmp_path):
    fc, out = _forecast(_bundle(tmp_path, offset=0.1))
    _, plain = _forecast(None)
    assert fc.model_info() == {"model_version": fc._corrector.version, "model_ready": True}
    g = out["gate_x"]
    assert g["source"] == "local_model" and plain["gate_x"]["source"] == "twin_model"
    for p, q in zip(g["points"], plain["gate_x"]["points"]):
        assert p["predicted_utilisation"] == pytest.approx(q["predicted_utilisation"] + 0.1, abs=1e-3)
        assert p["lower_90"] <= p["predicted_utilisation"] <= p["upper_90"]
    assert g["time_to_critical_sec"] == fc._time_to_critical(g["baseline_value"], g["points"], "gate_x")
    assert out["zone_y"]["source"] == "persistence"      # no twin-model forecast, nothing to correct


def test_forecaster_keeps_the_twin_model_when_the_bundle_is_refused(tmp_path):
    fc, out = _forecast(_bundle(tmp_path, passes=False))
    assert out["gate_x"]["source"] == "twin_model"
    assert fc.model_info()["model_ready"] is False and "gate" in fc._correction_error


# --- time_to_critical on the twin's dense, age-aligned projection ------------------------------
def _ramp_prior(slope_per_min: float, start: float, computed_at: str) -> dict:
    """A plan rising linearly, sampled every 60 s out to 3720 s."""
    path = [start + slope_per_min * (k + 1) for k in range(62)]
    return {"now": start, "points": {h: start + slope_per_min * h / 60 for h in HORIZONS}, "path": path,
            "path_step_sec": 60, "computed_at": computed_at}


def test_a_reused_projection_is_read_at_its_age():
    from app.ml_reference.forecaster import Forecaster

    fc = Forecaster({})
    a = fc.aligned_prior(_ramp_prior(0.001, 0.3, "2026-09-04T15:00:00Z"), "2026-09-04T15:01:30Z")   # 90 s old
    assert a["now"] == pytest.approx(0.3 + 0.0015)
    assert a["points"][1800] == pytest.approx(0.3 + 0.001 * (1800 + 90) / 60)
    assert a["path"][0] == (0, pytest.approx(a["now"])) and a["path"][-1][0] == 3600


def test_time_to_critical_finds_a_crossing_between_horizons():
    """A surge over the line from 20 to 25 min is invisible at 15 / 30 / 60 min."""
    from app.ml_reference.forecaster import Forecaster

    fc = Forecaster({"critical_utilisation": 0.9})
    path = [0.95 if 1200 <= 60 * (k + 1) <= 1500 else 0.5 for k in range(62)]
    prior = {"now": 0.5, "points": {h: 0.5 for h in HORIZONS}, "path": path, "path_step_sec": 60,
             "computed_at": "2026-09-04T15:00:00Z"}
    out = fc.predict({"gate_x": [0.5] * 12}, {"gate_x": 1000.0}, list(HORIZONS), "2026-09-04T15:00:00Z",
                     model_prior={"gate_x": prior})["gate_x"]
    assert all(p["predicted_utilisation"] < 0.9 for p in out["points"])
    assert 1140 < out["time_to_critical_sec"] <= 1200


def test_corrected_path_interpolates_the_correction():
    from app.ml_reference.forecaster import Forecaster

    path = [(t, 0.5) for t in range(0, 3601, 60)]
    out = dict(Forecaster.corrected_path(path, [900, 1800, 3600], [0.5, 0.5, 0.5], [0.6, 0.5, 0.7]))
    assert out[0] == 0.5 and out[420] == pytest.approx(0.5 + 0.1 * 420 / 900) and out[900] == pytest.approx(0.6)
    assert out[1800] == pytest.approx(0.5) and out[2700] == pytest.approx(0.6) and out[3600] == pytest.approx(0.7)


def test_the_configured_correction_bundle_passed_its_gate():
    """What config serves is a verified bundle whose own eval.json passed the
    03 §2.5 gate, and the published evidence is that same evaluation."""
    pytest.importorskip("lightgbm")   # optional: requirements-ml.txt
    from app.config import get_config

    configured = get_config().raw["forecaster"].get("correction_artifact")
    if not configured:
        pytest.skip("no correction configured: the twin model is served")
    repo = Path(__file__).resolve().parents[2]
    corrector = ForecastCorrector(repo / configured)            # raises unless verified and passed
    report = json.loads((repo / configured / "eval.json").read_text(encoding="utf-8"))
    assert report["gate"]["passes"] and all(report["gate"]["checks"].values())
    assert report["gate"]["limits"]["ttc_max_error_sec"] == \
        get_config().raw["forecaster"]["correction_gate"]["ttc_max_error_sec"]
    demo = report["results"]["demo_scenario"]
    assert demo["model"]["ttc_error_sec"] <= 180 and demo["model"]["mae_1800"] <= demo["twin_model"]["mae_1800"]
    assert report["latency_ms"]["p95"] < report["latency_ms"]["forecast_budget_ms"]
    published = repo / "ML" / "evaluation" / "results" / f"{corrector.manifest['model_version']}.json"
    assert json.loads(published.read_text(encoding="utf-8")) == report
