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
    """Mean % reduction of a quantity vs the do-nothing world, over settled
    interventions (each measured at its own settlement moment)."""
    vals = []
    for st in store.settlements:
        cf, actual = float(st[cf_key]), float(st[actual_key])
        if cf > 1e-9:
            vals.append((cf - actual) / cf * 100.0)
    return round(sum(vals) / len(vals), 1) if vals else 0.0


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
    coverage = getattr(engine.registry.twin, "last_coverage", None)
    coverage = float(coverage) if coverage is not None else 0.0

    # Genuinely counterfactual: each settled intervention's live city against
    # the do-nothing fork taken at approval (engine._settle_executing_interventions).
    variance_reduction = _settlement_reduction(store, "zone_variance_actual", "zone_variance_counterfactual")
    peak_reduction = _settlement_reduction(store, "root_actual", "root_counterfactual")

    # 03 §5.6 names two distinct metrics that used to be conflated into one
    # field: convergence rate (mechanical — did the solver converge) and
    # certificate accuracy (does the converged prediction agree with an
    # independent twin.branch() rollout, within 15%). `certificates_scored` is
    # written once per certified candidate in
    # `Engine._score_certificate_accuracy` and is exactly the latter.
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

    ops = store.operations or {}
    states = store.entity_states
    crowd_utils = [s["utilisation"] for e, s in states.items() if store.nodes[e]["entity_type"] != "hotel"]
    settled = store.settlements
    realised = [e["realised_relief_pct"] for e in store.regret_entries]
    operations = {
        "capacity_utilisation": {"value": round(sum(crowd_utils) / len(crowd_utils), 4) if crowd_utils else 0.0},
        "peak_congestion": {"value": round(max(crowd_utils), 4) if crowd_utils else 0.0,
                            "target": engine.config.critical_utilisation},
        "critical_locations": {"value": float(store.summary.get("critical_count", 0)), "target": 0.0},
        "queued_people": {"value": float(ops.get("queued_people", 0.0))},
        "avg_travel_time_sec": {"value": float(ops.get("avg_travel_time_sec", 0.0))},
        "late_entries": {"value": float(ops.get("late_entries", 0.0)), "target": 0.0},
        "unmet_room_requests": {"value": float(ops.get("rooms_unmet", 0.0)), "target": 0.0},
        "rooms_available": {"value": float(ops.get("rooms_available", 0.0))},
        "saturated_hotels": {"value": float(ops.get("saturated_properties", 0.0))},
        "visitors_redirected": {"value": float(ops.get("diverted_people", 0.0))},
        "interventions_settled": {"value": float(len(settled))},
        "mean_realised_relief_pct": {
            "value": round(sum(realised) / len(realised), 1) if realised else 0.0,
            "baseline_name": "do_nothing_counterfactual",
        },
        "attendee_compliance": {"value": engine.current_compliance(),
                                "baseline": float(engine.icfg.get("default_compliance", 0.6)),
                                "baseline_name": "configured_prior"},
    }

    return {
        "sim_time": store.sim_time,
        "operations": operations,
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
                "value": precision,
            },
            "cascade_recall": {
                "value": recall,
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
                "baseline_name": "twin_branch_ground_truth",
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
    regrets = [e["regret"] for e in entries]
    count = len(regrets)
    mean_abs = round(sum(abs(r) for r in regrets) / count, 2) if count else 0.0

    # Least-squares slope over the ledger; negative means we are getting better.
    slope = 0.0
    if count >= 2:
        mean_x = (count - 1) / 2.0
        mean_y = sum(regrets) / count
        denom = sum((i - mean_x) ** 2 for i in range(count))
        if denom:
            slope = round(
                sum((i - mean_x) * (regrets[i] - mean_y) for i in range(count)) / denom, 3
            )

    return {
        "entries": entries,
        "summary": {"count": count, "mean_absolute_regret": mean_abs, "trend_slope": slope},
    }
