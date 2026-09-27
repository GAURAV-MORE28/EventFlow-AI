"""ML honesty (audit Phase 0): what the API says about models must match what
actually produced the values.

  * `/health` reports the same cascade source the published cascades carry.
  * Root-step "confidence" is never arithmetic dressed up as a model output.
  * In shadow mode the cascade model's output reaches no published payload.
  * A forecast's `baseline_comparison` scores the model that produced it.
  * A timed-out candidate evaluation cannot write into the candidates afterwards.
"""
from __future__ import annotations

import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.ml_reference.forecaster import Forecaster
from app.services.cascade_flow import ml_confidence
from app.services.engine import get_engine

API = "/api/v1"


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
            loop.run_until_complete(engine.demo_control("reset", None, None, None, None))
        finally:
            loop.close()
        _run(engine, 120)  # 15:00 — arrival wave building, cascades active
        yield c


def test_health_cascade_source_matches_published_cascades(client):
    health = client.get(f"{API}/health").json()["modules"]["cascade"]
    active = client.get(f"{API}/cascade/active").json()
    assert health["active_source"] == active["source"]
    for c in active["cascades"]:
        assert c["source"] == active["source"]
    assert health["gnn_mode"] == get_engine().cascade_ml_mode()


def test_root_steps_never_carry_arithmetic_confidence():
    gnn_output = [{
        "root_entity_id": "metro_b", "source": "gnn",
        "steps": [
            {"entity_id": "metro_b", "depth": 0, "failure_probability": 0.99},   # util / critical line
            {"entity_id": "gate_3", "depth": 1, "failure_probability": 0.71},
        ],
    }]
    assert ml_confidence(gnn_output) == {"gate_3": 0.71}
    # A deterministic result is never read as model output.
    assert ml_confidence([{**gnn_output[0], "source": "deterministic"}]) == {}


def test_shadow_mode_keeps_model_output_out_of_published_cascades(client):
    engine = get_engine()
    if engine.cascade_ml_mode() == "off":
        pytest.skip("no ML cascade model loaded")
    cfg = engine.config.raw["cascade"]
    original = cfg.get("gnn_mode")
    cfg["gnn_mode"] = "shadow"
    try:
        _run(engine, 2)
        assert engine.store.cascade_ml["mode"] == "shadow"
        assert engine.store.cascade_ml["generated_at"] == engine.store.sim_time
        for c in client.get(f"{API}/cascade/active").json()["cascades"]:
            assert c["ml_enhanced"] is False
            assert all(s["confidence"] is None for s in c["steps"])

        cfg["gnn_mode"] = "annotate"
        _run(engine, 2)
        nodes = engine.store.cascade_ml["nodes"]
        horizons = (900, 1800, 3600)
        for c in client.get(f"{API}/cascade/active").json()["cascades"]:
            for s in c["steps"]:
                # Every published confidence is exactly the model's probability at
                # the first horizon covering the step's eta.
                if s["confidence"] is not None:
                    h = next((h for h in horizons if s["eta_sec"] <= h), 3600)
                    assert s["confidence"] == nodes[s["entity_id"]][f"p_fail_{h}"]
    finally:
        cfg["gnn_mode"] = original


def test_baseline_comparison_scores_the_model_that_produced_the_forecast():
    """The twin-model forecast is exact here (its 900s point is always what is
    observed 900s later), persistence is off by 30 steps of drift. The
    comparison must say so — the old one-step trend refit could not."""
    fc = Forecaster({"horizons_sec": [900, 1800, 3600], "step_sec": 30, "tsfm_enabled": False})
    drift = 0.001   # per 30s step
    history: list[float] = []
    t0 = 1_788_000_000
    out = {}
    for k in range(80):
        history.append(0.3 + drift * k)
        last = history[-1]
        prior = {"now": last, "points": {h: last + drift * h / 30 for h in (900, 1800, 3600)}}
        sim_time = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t0 + 30 * k))
        out = fc.predict({"e": list(history)}, {"e": 100.0}, None, sim_time, model_prior={"e": prior})
    forecast = out["e"]
    assert forecast["source"] == "twin_model"
    cmp_ = forecast["baseline_comparison"]
    assert cmp_ is not None
    assert cmp_["model_mae"] == pytest.approx(0.0, abs=1e-4)
    assert cmp_["persistence_mae"] == pytest.approx(drift * 30, abs=1e-4)
    assert cmp_["improvement_pct"] == pytest.approx(100.0, abs=0.5)


def test_baseline_comparison_resets_when_the_clock_moves_back():
    fc = Forecaster({"horizons_sec": [900, 1800, 3600], "step_sec": 30, "tsfm_enabled": False})
    t0 = 1_788_000_000
    history: list[float] = []
    for k in range(60):
        history.append(0.5)
        prior = {"now": 0.5, "points": {900: 0.5, 1800: 0.5, 3600: 0.5}}
        fc.predict({"e": list(history)}, {"e": 1.0}, None,
                   time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t0 + 30 * k)), model_prior={"e": prior})
    assert fc._scored
    fc.predict({"e": [0.5, 0.5]}, {"e": 1.0}, None,
               time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t0)), model_prior={"e": prior})
    assert not fc._scored


def test_timed_out_candidate_evaluation_cannot_write_late(client, monkeypatch):
    from app.services import evaluation

    engine = get_engine()
    finished = asyncio.Event()

    def slow(_engine, candidates, _root, _compliance):
        time.sleep(0.3)
        for c in candidates:
            c["estimated_relief_pct"] = 999.0
        loop.call_soon_threadsafe(finished.set)

    monkeypatch.setattr(evaluation, "evaluate_candidates", slow)
    monkeypatch.setitem(engine.config.raw["budgets_ms"], "evaluate", 50)
    candidates = [{"intervention_id": "int_test", "estimated_relief_pct": 12.0}]

    async def scenario():
        result = await engine._evaluate_safely(candidates, "metro_b")
        await asyncio.wait_for(finished.wait(), timeout=5)   # the late write has happened
        return result

    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(scenario())
    finally:
        loop.close()
    assert result is candidates
    assert candidates[0]["estimated_relief_pct"] == 12.0
