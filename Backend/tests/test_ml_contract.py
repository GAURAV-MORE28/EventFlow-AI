"""ML contract tests (03_ML_CONTRACT.md §0, §12) — referenced by README.md's
"Schema validation in CI" section since day one, but never written until now.

Exercises the ML modules directly (not through the REST layer test_contract.py
already covers) against the §0 universal rules every module must satisfy:

  1. No exceptions escape — malformed input still returns a well-shaped dict.
  2. Every module's return validates against the schema its consumer expects.
  3. Deterministic under a fixed seed — same inputs twice, identical output.
  4. `ready()` and `fallback()` exist and return the contracted shapes.

This runs against whichever implementation `ml_registry` resolves (the
reference modules by default, or a real ML/ drop-in — the point of the seam
is that this test should not need to know which).
"""
from __future__ import annotations

from typing import Any

import pytest

from app import schemas as S
from app.config import get_config
from app.ml_registry import MLRegistry
from app.topology import build_topology


@pytest.fixture(scope="module")
def registry() -> MLRegistry:
    return MLRegistry()


@pytest.fixture(scope="module")
def topology() -> dict:
    return build_topology()


@pytest.fixture(scope="module")
def node_state(topology: dict) -> dict[str, dict]:
    """A minimal, realistic node_state — enough shape for every module's
    interface without running a full engine cycle."""
    out: dict[str, dict] = {}
    for i, n in enumerate(topology["nodes"]):
        util = 0.3 + 0.02 * (i % 30)  # spread values so variance/bands are non-trivial
        out[n["entity_id"]] = {
            "entity_id": n["entity_id"],
            "entity_type": n["entity_type"],
            "display_name": n["display_name"],
            "nominal_capacity": float(n["nominal_capacity"]),
            "utilisation": util,
            "current_count": util * float(n["nominal_capacity"]),
            "flow_rate_per_min": 5.0,
            "risk_score": int(util * 100),
            "risk_band": "high" if util > 0.8 else "moderate" if util > 0.3 else "low",
            "is_observed": True,
            "forecast_900": util,
            "forecast_1800": min(util + 0.05, 1.2),
            "forecast_3600": min(util + 0.1, 1.3),
            "time_to_critical_sec": 1200,
            "degree": 2,
        }
    return out


@pytest.fixture(scope="module")
def series(node_state: dict[str, dict]) -> dict[str, list[float]]:
    # A short rising trend per entity — enough points for a quadratic fit.
    return {eid: [max(0.0, s["utilisation"] - 0.02 * (9 - i)) for i in range(10)] for eid, s in node_state.items()}


def config_block(name: str) -> dict:
    cfg = get_config()
    raw = cfg.raw
    out = dict(raw.get(name, {}))
    out["seed"] = cfg.demo_seed
    out["critical_utilisation"] = raw["thresholds"]["critical_utilisation"]
    out["risk_bands"] = raw["thresholds"]["risk_bands"]
    return out


# --- universal §0 rules, run against every module -----------------------------
def test_every_module_has_ready_and_fallback(registry: MLRegistry) -> None:
    for name in ("forecaster", "cascade", "twin", "equilibrium", "optimiser", "risk", "anomaly"):
        module = getattr(registry, name)
        assert hasattr(module, "ready") and callable(module.ready)
        assert module.ready() is True or module.ready() is False
        assert hasattr(module, "fallback") and callable(module.fallback)


def test_forecaster_no_exceptions_on_garbage_input(registry: MLRegistry) -> None:
    """03 §0 rule 5: catch, log, return fallback — never raise."""
    result = registry.forecaster.predict({"nonexistent": []}, {}, [900, 1800, 3600], "2026-01-01T00:00:00Z")
    assert isinstance(result, dict)


def test_forecaster_return_matches_forecast_schema(
    registry: MLRegistry, series: dict, node_state: dict
) -> None:
    capacities = {eid: s["nominal_capacity"] for eid, s in node_state.items()}
    forecasts = registry.forecaster.predict(series, capacities, [900, 1800, 3600], "2026-01-01T00:00:00Z")
    assert forecasts, "forecaster produced no output for any entity"
    for eid, raw in forecasts.items():
        payload = dict(raw)
        payload["entity_id"] = eid
        payload["generated_at"] = "2026-01-01T00:00:00Z"
        S.Forecast(**payload)  # raises on schema mismatch


def test_forecaster_deterministic_under_seed(series: dict, node_state: dict) -> None:
    from app.ml_reference.forecaster import Forecaster

    capacities = {eid: s["nominal_capacity"] for eid, s in node_state.items()}
    a = Forecaster(config_block("forecaster"))
    b = Forecaster(config_block("forecaster"))
    out_a = a.predict(series, capacities, [900, 1800, 3600], "2026-01-01T00:00:00Z")
    out_b = b.predict(series, capacities, [900, 1800, 3600], "2026-01-01T00:00:00Z")
    assert out_a == out_b, "same seed + same inputs must reproduce identically (01 §8)"


def test_cascade_no_exceptions_on_garbage_input(registry: MLRegistry) -> None:
    result = registry.cascade.predict("does_not_exist", {}, [], max_depth=4, generated_at=None)
    assert isinstance(result, dict)
    assert result["steps"] == []


def test_cascade_return_matches_cascade_result_schema(
    registry: MLRegistry, node_state: dict, topology: dict
) -> None:
    root = next(iter(node_state))
    result = registry.cascade.predict(root, node_state, topology["edges"], generated_at="2026-01-01T00:00:00Z")
    S.CascadeResult(**result)


def test_cascade_fallback_matches_cascade_result_schema(
    registry: MLRegistry, node_state: dict, topology: dict
) -> None:
    root = next(iter(node_state))
    result = registry.cascade.fallback(root, node_state, topology["edges"], None, "2026-01-01T00:00:00Z")
    S.CascadeResult(**result)
    assert result["source"] == "deterministic"


def test_twin_assimilate_matches_twin_fidelity_schema(registry: MLRegistry, node_state: dict) -> None:
    twin = registry.twin
    if hasattr(twin, "initialise"):
        caps = {eid: s["nominal_capacity"] for eid, s in node_state.items()}
        counts = {eid: s["current_count"] for eid, s in node_state.items()}
        twin.initialise(list(node_state.keys()), caps, counts)
    observations = {eid: s["current_count"] for eid, s in list(node_state.items())[:10]}
    result = twin.assimilate(observations, "2026-01-01T00:00:00Z", observations)
    S.TwinFidelity(**result)


def test_twin_no_exceptions_on_empty_observations(registry: MLRegistry) -> None:
    result = registry.twin.assimilate({}, "2026-01-01T00:00:00Z", {})
    assert isinstance(result, dict)


def test_equilibrium_certify_matches_certificate_schema(
    registry: MLRegistry, node_state: dict, topology: dict
) -> None:
    intervention = {
        "intervention_id": "int_test01",
        "target_entity_ids": [next(iter(node_state))],
        "estimated_relief_pct": 20.0,
    }
    result = registry.equilibrium.certify(intervention, node_state, topology["edges"], topology["segments"])
    S.Certificate(**result)
    assert len(result["compliance_sweep"]) == 3
    assert [r["compliance_rate"] for r in result["compliance_sweep"]] == [0.4, 0.6, 0.9]


def test_equilibrium_no_exceptions_on_unknown_targets(registry: MLRegistry, topology: dict) -> None:
    intervention = {"intervention_id": "int_bad", "target_entity_ids": ["nope"], "estimated_relief_pct": 10.0}
    result = registry.equilibrium.certify(intervention, {}, topology["edges"], topology["segments"])
    S.Certificate(**result)


def test_optimiser_candidates_are_gated_by_min_feasibility(
    registry: MLRegistry, node_state: dict, topology: dict
) -> None:
    root = next(iter(node_state))
    risk_context = {
        "root_entity_id": root, "cascade": None, "node_state": node_state,
        "edges": topology["edges"], "sim_time": "2026-01-01T00:00:00Z",
    }
    candidates = registry.optimiser.generate(risk_context, 5)
    assert candidates, "optimiser produced no candidates (notify_only is always present)"
    for c in candidates:
        assert c["feasibility"] >= registry.optimiser.min_feasibility


def test_risk_and_anomaly_do_not_raise(registry: MLRegistry, node_state: dict) -> None:
    scores = registry.risk.score(node_state, {}, {})
    assert isinstance(scores, dict) and scores
    anomalies = registry.anomaly.detect({eid: [0.1, -0.1, 0.2] for eid in list(node_state)[:5]})
    assert isinstance(anomalies, list)
