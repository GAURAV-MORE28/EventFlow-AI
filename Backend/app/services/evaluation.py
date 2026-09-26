"""Simulate every candidate intervention before an operator sees it.

`estimated_relief_pct` used to be a constant per template. Now each candidate is
applied to its own clone of the live city and run forward
(`interventions.evaluation_horizon_sec`) next to a do-nothing clone; the relief
is the measured drop in the triggering entity's peak utilisation, and the
evaluation also reports any entity the action would push over the critical
line. The certificate (EquilibriumSolver) remains the independent,
game-theoretic check; the simulation is the physical one.
"""
from __future__ import annotations

from typing import Any

from ..ml_reference.common import clamp
from .projection import SKIP_PEAK_TYPES, run_forward


def _network_peak(peaks: dict[str, float], types: dict[str, str]) -> float:
    vals = [u for e, u in peaks.items() if types[e] not in SKIP_PEAK_TYPES]
    return max(vals) if vals else 0.0


def evaluate_candidates(engine: Any, candidates: list[dict], root_id: str, compliance: float) -> None:
    cfg = engine.config.raw.get("interventions", {})
    horizon = float(cfg.get("evaluation_horizon_sec", 1800))
    step = int(cfg.get("evaluation_step_sec", 60))
    duration = float(cfg.get("effect_duration_sec", 2700))
    critical = getattr(engine, "critical_lines", None) or engine.config.critical_utilisation

    with engine.world_lock:
        base_gen = engine.generator.clone()
        clones = [engine.generator.clone() for _ in candidates]
    types = base_gen.types
    base = run_forward(base_gen, horizon, step, int(horizon), critical)
    b_root = base["peaks"].get(root_id, 0.0)
    b_net = _network_peak(base["peaks"], types)

    for cand, gen in zip(candidates, clones):
        if cand["intervention_type"] != "notify_only":
            gen.apply_intervention(cand, compliance, duration)
        res = run_forward(gen, horizon, step, int(horizon), critical)
        a_root = res["peaks"].get(root_id, 0.0)
        relief = (b_root - a_root) / b_root * 100.0 if b_root > 0.05 else 0.0
        new_critical = sorted(res["critical"] - base["critical"])
        moved = res["stats"]["diverted_people"] - base["stats"]["diverted_people"]
        cand["estimated_relief_pct"] = round(clamp(relief, -100.0, 100.0), 1)
        cand["evaluation"] = {
            "horizon_sec": int(horizon),
            "compliance": round(compliance, 3),
            "root_entity_id": root_id,
            "root_peak_before": round(b_root, 4),
            "root_peak_after": round(a_root, 4),
            "network_peak_before": round(b_net, 4),
            "network_peak_after": round(_network_peak(res["peaks"], types), 4),
            "critical_before": len(base["critical"]),
            "critical_after": len(res["critical"]),
            "new_critical_entities": new_critical[:6],
            "people_redirected": int(round(max(0.0, moved))),
            "travel_time_delta_sec": round(res["stats"]["avg_travel_time_sec"] - base["stats"]["avg_travel_time_sec"], 1),
            "rooms_unmet_delta": round(res["stats"]["rooms_unmet"] - base["stats"]["rooms_unmet"], 1),
        }
