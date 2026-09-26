"""Phase 1E — risk capacity invariant (FINAL_AUDIT_REPORT P0-06).

Before Phase 1: 1.00 / 1.05 / 1.20 utilisation scored MODERATE, 1.50 HIGH.
"""
from __future__ import annotations

import asyncio
import math

import pytest

from app.config import get_config
from app.db.base import create_all
from app.db.seed import clear_run_tables
from app.ml_reference.common import band_from_score
from app.ml_reference.risk import RiskScorer
from app.services.engine import Engine

CFG = get_config().raw
BANDS = CFG["thresholds"]["risk_bands"]


def _scorer() -> RiskScorer:
    return RiskScorer({**CFG["risk"], "risk_bands": BANDS})


def _score(util, forecast_1800=None, exposure=0.0, scorer=None):
    fc = None if forecast_1800 is None else {"points": [{"horizon_sec": 1800, "predicted_utilisation": forecast_1800}]}
    return (scorer or _scorer()).score({"x": {"utilisation": util, "entity_type": "gate"}},
                                       {"x": fc} if fc else {}, {"x": exposure})["x"]


@pytest.mark.parametrize("util,expected", [
    (0.50, (25, "low")), (0.60, (30, "low")), (0.80, (40, "moderate")),
    (0.90, (45, "moderate")), (0.99, (50, "moderate")),
    (1.00, (81, "critical")), (1.05, (81, "critical")), (1.20, (81, "critical")), (1.50, (81, "critical")),
])
def test_boundary_table_flat_forecast(util, expected):
    r = _score(util, forecast_1800=util)
    assert (r["risk_score"], r["risk_band"]) == expected


def test_below_capacity_stays_differentiated():
    scores = [_score(u, forecast_1800=u)["risk_score"] for u in (0.5, 0.6, 0.8, 0.9, 0.99)]
    assert scores == sorted(scores) and len(set(scores)) == len(scores)


@pytest.mark.parametrize("util", [1.0, 1.05, 1.2, 1.5, 2.0])
@pytest.mark.parametrize("forecast", [0.2, 1.0, 1.9])
@pytest.mark.parametrize("exposure", [0.0, 1.0])
def test_over_capacity_is_always_critical_whatever_the_other_inputs(util, forecast, exposure):
    r = _score(util, forecast_1800=forecast, exposure=exposure)
    assert r["risk_band"] == "critical" and r["risk_score"] >= 81, r


def test_score_and_band_never_disagree():
    scorer = _scorer()
    for i in range(0, 201):
        for forecast in (None, 0.0, 1.2):
            for exposure in (0.0, 0.5, 1.0):
                r = _score(i / 100, forecast_1800=forecast, exposure=exposure, scorer=scorer)
                assert r["risk_band"] == band_from_score(r["risk_score"], BANDS), (i, forecast, exposure, r)
                assert 0 <= r["risk_score"] <= 100


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), None, "x"])
def test_non_finite_inputs_are_missing_not_alarms(bad):
    r = _score(bad)
    assert r["risk_score"] == 0 and r["risk_band"] == "low"    # NaN used to clamp to 100/critical
    r = _score(0.7, forecast_1800=bad, exposure=bad)
    assert r == _score(0.7)


def test_fallback_path_respects_the_invariant():
    out = _scorer().fallback({"a": {"utilisation": 1.02}, "b": {"utilisation": 0.4}})
    assert out["a"]["risk_band"] == "critical" and out["a"]["risk_score"] >= 81
    assert out["b"]["risk_band"] == band_from_score(out["b"]["risk_score"], BANDS)


def test_live_run_never_shows_over_capacity_below_critical():
    create_all()
    clear_run_tables()
    engine = Engine()
    loop = asyncio.new_event_loop()
    over = 0
    try:
        for _ in range(300):
            loop.run_until_complete(engine.run_cycle())
            for st in engine.store.entity_states.values():
                assert math.isfinite(st["utilisation"])
                assert st["risk_band"] == band_from_score(st["risk_score"], BANDS), st
                if st["utilisation"] >= 1.0:
                    over += 1
                    assert st["risk_band"] == "critical", st
    finally:
        loop.close()
    assert over > 0, "premise: the run does reach over-capacity states"
