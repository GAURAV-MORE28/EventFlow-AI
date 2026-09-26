"""The judging panel (01_BACKEND_CONTRACT.md §3.10).

Every value carries its baseline, because a bare number proves nothing. Where a
metric has not accumulated enough evidence yet, it reports its current honest
value rather than a flattering placeholder — including
`prediction.forecast_mae`, which is allowed to show the model losing to
persistence (03 §2.3).
"""
from __future__ import annotations

from typing import Any

from ..ml_reference.common import variance


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _improvement(value: float, baseline: float) -> float:
    if baseline in (0, None):
        return 0.0
    return round((baseline - value) / baseline * 100.0, 1)


def _rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 3) if denominator else 0.0


def _settlement_reduction(store: Any, actual_key: str, cf_key: str) -> float:
    """Mean % reduction vs the MATCHED do-nothing world over settled
    interventions (engine._settle_executing_interventions): both values come
    from the same event-world model at the same instant. 0.0 when nothing has
    settled yet."""
    values = []
    for s in store.settlements:
        cf, actual = s[cf_key], s[actual_key]
        if cf > 1e-9:
            values.append((cf - actual) / cf * 100.0)
    return round(sum(values) / len(values), 1) if values else 0.0


def build_metrics(engine: Any) -> dict[str, Any]:
    store = engine.store
    twin = store.twin_fidelity or {}

    model_mae = round(_mean(store.forecast_errors["model"][-200:]), 4)
    persistence_mae = round(_mean(store.forecast_errors["persistence"][-200:]), 4)

    lead_time = round(_mean(store.cascade_lead_times[-50:]), 0)
    # Measured online (engine.py `_resolve_cascade_predictions` /
    # `_track_cascade_recall`), never a formula over `cascade_count`. Precision
    # is over alerts (did each prediction come true by its own eta); recall is
    # over events (did each real high/critical transition have a prior alert)
    # — the standard split for streaming-alert evaluation, so the two
    # denominators are not the same count by design. In-simulation only, never
    # presented as field validity (03 §8.4).
    ev = store.cascade_eval
    precision = _rate(ev["alerts_confirmed"], ev["alerts_confirmed"] + ev["alerts_false"])
    recall = _rate(ev["events_caught"], ev["events_caught"] + ev["events_missed"])

    assimilated = float(twin.get("assimilated_rmse") or 0.0)
    uncorrected = twin.get("uncorrected_rmse")
    # Measured by the twin (fraction of entities whose simulated ground truth
    # falls inside the ensemble's 90% interval) — it used to be the formula
    # clamp(0.85 + spread, 0.80, 0.95), which could never leave its range.
    coverage = twin.get("ensemble_coverage")
    coverage = round(float(coverage), 2) if coverage is not None else 0.0

    # Matched counterfactual (Phase 1B): for each settled intervention, the
    # affected entities' peak and the zone variance in the live world vs the
    # same world with that intervention's effects excluded, same instant.
    peak_reduction = _settlement_reduction(store, "actual_peak", "counterfactual_peak")
    variance_reduction = _settlement_reduction(store, "actual_zone_variance", "counterfactual_zone_variance")

    # 03 §5.6 certificate accuracy (Phase 1D): for each settled intervention,
    # the certified world-model projection of its sources' utilisation made at
    # approval vs the realised value at settlement, within 15%. Both come from
    # the same event-world model, so disagreement comes only from events after
    # approval (other approvals, injected disruptions) — it is a consistency
    # check of the simulation, not evidence of field accuracy.
    certified = [
        i for i in store.interventions.values() if (i.get("certificate") or {}).get("converged")
    ]
    convergence_rate = (
        round(len(certified) / len(store.interventions) * 100.0, 1) if store.interventions else 0.0
    )
    certificate_accuracy = (
        round(_mean([1.0 if ok else 0.0 for ok in store.certificates_scored]) * 100.0, 1)
        if store.certificates_scored else 0.0
    )

    ungrounded_rate = (
        round(store.commander_ungrounded / store.commander_calls, 3) if store.commander_calls else 0.0
    )
    tool_correctness = (
        round(store.commander_tool_calls_ok / store.commander_tool_calls_total, 2)
        if store.commander_tool_calls_total else 0.0
    )

    return {
        "sim_time": store.sim_time,
        "prediction": {
            "forecast_mae": {
                "value": model_mae, "baseline": persistence_mae,
                "baseline_name": "persistence",
                "improvement_pct": _improvement(model_mae, persistence_mae),
            },
            "cascade_lead_time_sec": {
                "value": lead_time, "baseline": 0.0, "baseline_name": "threshold_rule",
            },
            "cascade_precision": {
                "value": precision, "baseline": 0.31, "baseline_name": "random_propagation",
            },
            "cascade_recall": {
                "value": recall, "baseline": 0.30, "baseline_name": "random_propagation",
            },
        },
        "twin": {
            "rmse": {
                "value": round(assimilated, 2),
                "baseline": None if uncorrected is None else round(float(uncorrected), 2),
                "baseline_name": "uncorrected_abm",
                "improvement_pct": twin.get("improvement_pct"),
            },
            "ensemble_coverage": {"value": coverage, "target_range": [0.85, 0.95]},
        },
        "decision": {
            "peak_utilisation_reduction_pct": {
                "value": peak_reduction, "baseline_name": "do_nothing_counterfactual",
            },
            "load_variance_reduction_pct": {
                "value": variance_reduction, "baseline_name": "do_nothing_counterfactual",
            },
            "unstable_interventions_caught": {
                "value": float(store.unstable_caught),
                "baseline_name": "naive_optimiser_would_approve",
            },
            "certificate_accuracy_pct": {
                "value": certificate_accuracy, "target": 85.0,
                "baseline_name": "realised_at_settlement",
            },
            "convergence_rate_pct": {"value": convergence_rate, "target": 85.0},
        },
        "system": {
            "cycle_latency_ms": {
                "value": round(_mean(list(store.cycle_latency_ms)), 0), "target": 2000.0,
            },
            "commander_ungrounded_rate": {"value": ungrounded_rate, "target": 0.0},
            "tool_call_correctness": {"value": tool_correctness, "target": 0.90},
        },
    }


def build_regret(engine: Any) -> dict[str, Any]:
    entries = engine.store.regret_entries
    # Unavailable (null) regrets are listed but excluded from the aggregates.
    regrets = [e["regret"] for e in entries if e.get("regret") is not None]
    count = len(entries)
    k = len(regrets)
    mean_abs = round(sum(abs(r) for r in regrets) / k, 2) if k else 0.0

    # Least-squares slope over the ledger; negative means we are getting better.
    slope = 0.0
    if k >= 2:
        mean_x = (k - 1) / 2.0
        mean_y = sum(regrets) / k
        denom = sum((i - mean_x) ** 2 for i in range(k))
        if denom:
            slope = round(
                sum((i - mean_x) * (regrets[i] - mean_y) for i in range(k)) / denom, 3
            )

    return {
        "entries": entries,
        "summary": {"count": count, "mean_absolute_regret": mean_abs, "trend_slope": slope},
    }
