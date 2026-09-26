"""Phase 1A — Digital Twin numerical correctness (FINAL_AUDIT_REPORT P0-01).

Runs the real engine cycle (generator -> twin -> ...) in-process for 600
cycles once, checking the twin against the generator's ground truth at the
1 / 10 / 30 / 100 / 300 / 600-cycle checkpoints. Pre-Phase-1 values for the
same measurements are recorded in PHASE1_VALIDATION_REPORT.md §1A.
"""
from __future__ import annotations

import asyncio
import json
import math

import numpy as np
import pytest

from app.db.base import create_all
from app.db.seed import clear_run_tables
from app.services.engine import Engine

CHECKPOINTS = (1, 10, 30, 100, 300, 600)


def _run(cycles: int, *, drift_at: int | None = None, record: bool = False):
    create_all()
    clear_run_tables()
    engine = Engine()
    twin = engine.registry.twin
    unobserved = [k for k, v in engine.generator._observed.items() if not v]
    observed = [k for k, v in engine.generator._observed.items() if v]
    snapshots, worst = {}, {"nan": 0, "neg": 0, "flow_cap": 0.0, "uncorrected_min": math.inf}
    loop = asyncio.new_event_loop()
    try:
        for c in range(1, cycles + 1):
            if drift_at == c:
                engine.set_drift_mode(True)
            loop.run_until_complete(engine.run_cycle())
            X = twin._X
            n = twin.n
            worst["nan"] += int((~np.isfinite(X)).sum())
            worst["neg"] += int((X[:n] < 0).sum())
            worst["flow_cap"] = max(worst["flow_cap"], float((np.abs(X[n:]).max(axis=1) / twin.capacities).max()))
            if twin._X_uncorrected is not None:
                worst["uncorrected_min"] = min(worst["uncorrected_min"], float(twin._X_uncorrected[:n].min()))
            if record and c in CHECKPOINTS:
                truth = engine.generator.ground_truth()
                st = engine.store.entity_states
                un_err = [st[k]["utilisation"] - truth[k]["utilisation"] for k in unobserved]
                std_util = X[:n].std(axis=1) / twin.capacities
                idx = {k: i for i, k in enumerate(twin.entity_ids)}
                snapshots[c] = {
                    "unobs_rmse": math.sqrt(sum(e * e for e in un_err) / len(un_err)),
                    "unobs_maxabs": max(abs(e) for e in un_err),
                    "pinned": [k for k in unobserved
                               if (st[k]["utilisation"] <= 0.001 or st[k]["utilisation"] >= 1.999)
                               and 0.05 < truth[k]["utilisation"] < 1.5],
                    "spread_obs": float(np.mean([std_util[idx[k]] for k in observed])),
                    "spread_unobs": float(np.mean([std_util[idx[k]] for k in unobserved])),
                    "fidelity": dict(engine.store.twin_fidelity or {}),
                }
    finally:
        loop.close()
    return engine, snapshots, worst


@pytest.fixture(scope="module")
def long_run():
    return _run(600, drift_at=20, record=True)


def test_no_nan_inf_and_no_negative_counts(long_run):
    _, _, worst = long_run
    assert worst["nan"] == 0
    assert worst["neg"] == 0


def test_flows_stay_physically_bounded(long_run):
    # Steepest real ramp in the synthetic world is ~0.03 capacity/min; before
    # Phase 1 flows reached 13.5 capacities per minute.
    _, _, worst = long_run
    assert worst["flow_cap"] <= 0.05, worst["flow_cap"]


@pytest.mark.parametrize("cycle", CHECKPOINTS)
def test_unobserved_estimates_are_not_pinned_and_stay_close(long_run, cycle):
    _, snaps, _ = long_run
    s = snaps[cycle]
    assert s["pinned"] == [], f"cycle {cycle}: 0/2 artefacts {s['pinned']}"
    # Before Phase 1: RMSE 0.84 @100, 1.00 @300, 0.91 @600; max error 1.5.
    assert s["unobs_rmse"] <= 0.25, (cycle, s["unobs_rmse"])
    assert s["unobs_maxabs"] <= 0.6, (cycle, s["unobs_maxabs"])


@pytest.mark.parametrize("cycle", CHECKPOINTS)
def test_assimilated_rmse_does_not_explode(long_run, cycle):
    # Before Phase 1: 3,173 @100 -> 10,370 @600 people.
    _, snaps, _ = long_run
    assert snaps[cycle]["fidelity"]["assimilated_rmse"] <= 2000, snaps[cycle]["fidelity"]


def test_ensemble_spread_is_interpretable_and_coverage_is_measured(long_run):
    _, snaps, _ = long_run
    for cycle in (30, 100, 300, 600):
        s = snaps[cycle]
        # Estimated (unobserved) entities must carry MORE uncertainty than
        # sensor-backed ones — never the tiny-spread-while-wrong failure.
        assert s["spread_unobs"] > 3 * s["spread_obs"], (cycle, s)
        cov = s["fidelity"]["ensemble_coverage"]
        assert cov is not None and 0.75 <= cov <= 1.0, (cycle, cov)


def test_drift_baseline_is_bounded_and_improvement_is_honest(long_run):
    engine, snaps, worst = long_run
    fid = snaps[600]["fidelity"]
    assert fid["uncorrected_rmse"] is not None and math.isfinite(fid["uncorrected_rmse"])
    # The open-loop copy runs the same bounded model: before Phase 1 it reached
    # counts of -3e13 and RMSE 1.3e14, making any "improvement" meaningless.
    assert worst["uncorrected_min"] >= 0.0
    assert fid["uncorrected_rmse"] < 20_000
    expected = round((fid["uncorrected_rmse"] - fid["assimilated_rmse"]) / fid["uncorrected_rmse"] * 100, 1)
    assert fid["improvement_pct"] == pytest.approx(expected, abs=0.1)


def test_same_seed_is_repeatable():
    a, _, _ = _run(60)
    b, _, _ = _run(60)
    assert np.array_equal(a.registry.twin._X, b.registry.twin._X)
    assert json.dumps(a.store.entity_states, sort_keys=True) == json.dumps(b.store.entity_states, sort_keys=True)


def test_branch_never_mutates_live_state_or_consumes_live_rng():
    engine, _, _ = _run(40)
    twin = engine.registry.twin
    X_before = twin._X.copy()
    rng_before = json.dumps(twin._rng.bit_generator.state, sort_keys=True, default=str)
    twin.branch({"demand_multipliers": {"metro_b": 0.8}}, 1800)
    twin.branch({"capacity_multipliers": {"gate_3": 0.5}}, 3600)
    assert np.array_equal(X_before, twin._X)
    assert rng_before == json.dumps(twin._rng.bit_generator.state, sort_keys=True, default=str)


def test_branch_applies_demand_multiplier_once_not_every_step():
    engine, _, _ = _run(40)
    twin = engine.registry.twin
    out = twin.branch({"demand_multipliers": {"metro_b": 0.8}}, 1800)
    base = twin.branch({}, 1800)["trajectory"]["metro_b"]
    scen = out["trajectory"]["metro_b"]
    # Pre-Phase-1 compounding drove metro_b 1.19 -> 0.0 in six steps (0.8**6).
    ratio_last = scen[-1] / base[-1]
    assert 0.7 <= ratio_last <= 0.9, (base, scen)
