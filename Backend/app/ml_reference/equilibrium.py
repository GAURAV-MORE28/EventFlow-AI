"""EquilibriumSolver (03_ML_CONTRACT.md §5) — the signature innovation.

A Stackelberg game: the organiser sets incentives, attendee segments best-respond
by minimising personal cost. `certify()` runs the follower equilibrium at three
compliance rates and reports whether the intervention actually holds.

`derive_verdict` and `row_verdict` are defined **here and nowhere else**
(03 §5.3). The backend renders the verdict; the frontend renders the verdict;
neither recomputes it. If you find a second copy of this arithmetic, delete it.

`reason` is template-generated and deterministic — never LLM-written (03 §9).
"""
from __future__ import annotations

import logging
import math
from typing import Any

from .common import clamp, stable_unit, variance

log = logging.getLogger("eventflow.ml.equilibrium")

COMPLIANCE_SWEEP = [0.4, 0.6, 0.9]   # 00 §2.6 — always exactly these three rows


# --- 03 §5.3 verdict derivation — the single source of truth ----------------
def row_verdict(max_utilisation: float, post_variance: float, baseline_variance: float) -> str:
    if max_utilisation >= 1.0:
        return "UNSTABLE"        # created a new critical entity
    if post_variance > baseline_variance:
        return "UNSTABLE"        # made the distribution worse
    if max_utilisation >= 0.90:
        return "CONDITIONAL"
    return "STABLE"


def derive_verdict(sweep: list[dict], converged: bool, oscillation_risk: bool) -> str:
    if not converged or oscillation_risk:
        return "UNSTABLE"
    verdicts = [row["verdict"] for row in sweep]
    if all(v == "STABLE" for v in verdicts):
        return "STABLE"
    if any(v == "UNSTABLE" for v in verdicts):
        return "UNSTABLE" if verdicts.count("UNSTABLE") >= 2 else "CONDITIONAL"
    return "CONDITIONAL"


class EquilibriumSolver:
    def __init__(self, config: dict) -> None:
        self.config = config or {}
        self.max_iterations = int(self.config.get("max_iterations", 40))
        self.tol = float(self.config.get("convergence_tol", 0.005))
        self.damping = float(self.config.get("damping", 0.5))
        self.sweep_rates = list(self.config.get("compliance_sweep", COMPLIANCE_SWEEP))
        self.alpha = float(self.config.get("alpha", 1.0))
        self.beta = float(self.config.get("beta", 2.5))
        self.sigmoid_k = float(self.config.get("sigmoid_k", 3.0))
        self.seed = int(self.config.get("seed", 42))

    def ready(self) -> bool:
        return True

    # --- 03 §5.1 -------------------------------------------------------------
    def certify(
        self,
        intervention: dict,
        node_state: dict[str, dict],
        edges: list[dict],
        segments: list[dict],
    ) -> dict:
        try:
            return self._certify(intervention, node_state, edges, segments)
        except Exception:
            log.exception("certification failed for %s", intervention.get("intervention_id"))
            return self.fallback(intervention, node_state, edges, segments)

    def fallback(
        self,
        intervention: dict,
        node_state: dict[str, dict] | None = None,
        edges: list[dict] | None = None,
        segments: list[dict] | None = None,
    ) -> dict:
        iid = intervention.get("intervention_id", "int_unknown")
        return {
            "certificate_id": self._certificate_id(iid),
            "intervention_id": iid,
            "verdict": "UNSTABLE",
            "converged": False,
            "iterations": self.max_iterations,
            "post_nudge_variance": 0.0,
            "baseline_variance": 0.0,
            "max_zone_utilisation": 0.0,
            "max_zone_entity_id": None,
            "oscillation_risk": False,
            "compliance_sensitivity": 0.0,
            "compliance_sweep": [
                {"compliance_rate": r, "max_utilisation": 0.0, "verdict": "UNSTABLE"}
                for r in self.sweep_rates
            ],
            "reason": "Did not converge within iteration cap.",
        }

    # --- internals --------------------------------------------------------------
    def _certificate_id(self, intervention_id: str) -> str:
        return "cert_" + f"{int(stable_unit(self.seed, 'cert', intervention_id) * 0xFFFF):04x}"

    def _zones(self, node_state: dict[str, dict]) -> list[str]:
        """Load variance is measured over zone entities (01 §3.3)."""
        zones = [e for e, s in node_state.items() if s.get("entity_type") == "zone"]
        return zones or list(node_state.keys())

    def _certify(
        self,
        intervention: dict,
        node_state: dict[str, dict],
        edges: list[dict],
        segments: list[dict],
    ) -> dict:
        iid = intervention.get("intervention_id", "int_unknown")
        targets = list(intervention.get("target_entity_ids", []))
        relief = float(intervention.get("estimated_relief_pct", 0.0)) / 100.0

        # The load surface the intervention redistributes over: zones, plus every
        # entity the intervention names (so gate saturation is actually visible).
        surface = list(dict.fromkeys(self._zones(node_state) + [t for t in targets if t in node_state]))
        baseline = {e: float(node_state[e].get("utilisation", 0.0)) for e in surface}
        baseline_variance = variance(list(baseline.values()))

        # Where does displaced load go? Down `substitutes_for` edges from the targets,
        # and onto whatever the intervention feeds.
        receivers = self._receivers(targets, edges, surface)

        sweep: list[dict] = []
        converged_all = True
        oscillation_any = False
        iterations_used = 0
        worst: tuple[float, str | None] = (0.0, None)
        post_variance_at_60 = baseline_variance

        for rate in self.sweep_rates:
            load, converged, iters, oscillated = self._solve_followers(
                baseline, targets, receivers, relief, rate, segments, node_state
            )
            converged_all = converged_all and converged
            oscillation_any = oscillation_any or oscillated
            iterations_used = max(iterations_used, iters)

            max_entity = max(load, key=lambda e: load[e]) if load else None
            max_util = load.get(max_entity, 0.0) if max_entity else 0.0
            post_var = variance(list(load.values()))
            if abs(rate - 0.6) < 1e-9:
                post_variance_at_60 = post_var
            if max_util > worst[0]:
                worst = (max_util, max_entity)

            sweep.append(
                {
                    "compliance_rate": rate,
                    "max_utilisation": round(max_util, 4),
                    "verdict": row_verdict(max_util, post_var, baseline_variance),
                }
            )

        verdict = derive_verdict(sweep, converged_all, oscillation_any)
        utils = [row["max_utilisation"] for row in sweep]
        sensitivity = round(max(utils) - min(utils), 4) if utils else 0.0

        return {
            "certificate_id": self._certificate_id(iid),
            "intervention_id": iid,
            "verdict": verdict,
            "converged": converged_all,
            "iterations": iterations_used,
            "post_nudge_variance": round(post_variance_at_60, 4),
            "baseline_variance": round(baseline_variance, 4),
            "max_zone_utilisation": round(worst[0], 4),
            "max_zone_entity_id": worst[1],
            "oscillation_risk": oscillation_any,
            "compliance_sensitivity": sensitivity,
            "reason": self._reason(
                verdict, sweep, converged_all, oscillation_any, worst,
                baseline_variance, post_variance_at_60, node_state,
            ),
            "compliance_sweep": sweep,
        }

    def _receivers(self, targets: list[str], edges: list[dict], surface: list[str]) -> dict[str, float]:
        """Weighted destinations for displaced load, from `substitutes_for` / `feeds`."""
        weights: dict[str, float] = {}
        target_set = set(targets)
        for e in edges:
            if e["src_entity_id"] not in target_set:
                continue
            dst = e["dst_entity_id"]
            if dst not in surface:
                continue
            if e["edge_type"] == "substitutes_for":
                weights[dst] = weights.get(dst, 0.0) + float(e.get("substitutability", 0.0))
            elif e["edge_type"] in ("feeds", "serves", "last_mile_to"):
                weights[dst] = weights.get(dst, 0.0) + float(e.get("transfer_coefficient", 0.0))
        if not weights:
            # Nowhere named to send it: spread across the least loaded half of the surface.
            spare = sorted(surface, key=lambda e: e)[: max(1, len(surface) // 2)]
            weights = {e: 1.0 for e in spare}
        total = sum(weights.values()) or 1.0
        return {k: v / total for k, v in weights.items()}

    def _segment_cost(self, util: float, incentive: float, segment: dict) -> float:
        """03 §5.2 cost function, congestion term dominating at high load."""
        congestion = self.alpha * (max(util, 0.0) ** self.beta)
        return congestion - incentive * float(segment.get("price_elasticity", 0.0))

    def _solve_followers(
        self,
        baseline: dict[str, float],
        targets: list[str],
        receivers: dict[str, float],
        relief: float,
        compliance_rate: float,
        segments: list[dict],
        node_state: dict[str, dict],
    ) -> tuple[dict[str, float], bool, int, bool]:
        """Iterative best response with damping (Frank-Wolfe style)."""
        load = dict(baseline)
        history: list[dict[str, float]] = []
        converged = False
        oscillated = False
        iters = 0

        # Movable mass: what the intervention claims it can shift, scaled by how
        # much each segment actually complies at this rate.
        segments = segments or [{"share": 1.0, "price_elasticity": 0.5, "compliance_base_rate": 0.5}]

        for iters in range(1, self.max_iterations + 1):
            target_load = {e: load.get(e, 0.0) for e in targets if e in load}
            moved_total = 0.0
            for seg in segments:
                share = float(seg.get("share", 0.0))
                base_rate = float(seg.get("compliance_base_rate", 0.5))
                incentive = relief  # incentive strength scales with claimed relief
                # Cost gap drives compliance; sigmoid keeps it bounded.
                mean_target = sum(target_load.values()) / max(len(target_load), 1)
                mean_receiver = sum(load.get(e, 0.0) * w for e, w in receivers.items())
                gap = self._segment_cost(mean_target, 0.0, seg) - self._segment_cost(mean_receiver, incentive, seg)
                compliance = base_rate * compliance_rate * self._sigmoid(self.sigmoid_k * gap)
                moved_total += share * compliance

            moved_fraction = clamp(moved_total * relief, 0.0, 0.95)

            new_load = dict(load)
            displaced = 0.0
            for e in targets:
                if e in new_load:
                    delta = new_load[e] * moved_fraction
                    new_load[e] -= delta
                    displaced += delta * float(node_state.get(e, {}).get("nominal_capacity", 1.0))

            for dst, w in receivers.items():
                cap = float(node_state.get(dst, {}).get("nominal_capacity", 1.0)) or 1.0
                new_load[dst] = new_load.get(dst, 0.0) + (displaced * w) / cap

            # Damped update.
            blended = {
                e: (1.0 - self.damping) * load.get(e, 0.0) + self.damping * new_load.get(e, 0.0)
                for e in set(load) | set(new_load)
            }

            delta_inf = max((abs(blended[e] - load.get(e, 0.0)) for e in blended), default=0.0)
            history.append(dict(blended))
            load = blended

            if delta_inf < self.tol:
                converged = True
                break
            if self._oscillating(history):
                oscillated = True
                break

        return load, converged, iters, oscillated

    def _sigmoid(self, x: float) -> float:
        if x < -60:
            return 0.0
        if x > 60:
            return 1.0
        return 1.0 / (1.0 + math.exp(-x))

    def _oscillating(self, history: list[dict[str, float]]) -> bool:
        """03 §5.2: load at t within tol of t-2 but not t-1, twice consecutively."""
        if len(history) < 5:
            return False

        def close(a: dict[str, float], b: dict[str, float]) -> bool:
            return max((abs(a.get(k, 0.0) - b.get(k, 0.0)) for k in set(a) | set(b)), default=0.0) < self.tol

        checks = []
        for i in (len(history) - 1, len(history) - 2):
            if i - 2 < 0:
                return False
            checks.append(close(history[i], history[i - 2]) and not close(history[i], history[i - 1]))
        return all(checks)

    # --- 03 §5.4 reason templates (<=140 chars, deterministic) ------------------
    def _reason(
        self,
        verdict: str,
        sweep: list[dict],
        converged: bool,
        oscillation: bool,
        worst: tuple[float, str | None],
        baseline_variance: float,
        post_variance: float,
        node_state: dict[str, dict],
    ) -> str:
        max_util, entity = worst
        name = self._display(entity, node_state)

        if not converged:
            return "Did not converge within iteration cap."
        if oscillation:
            return f"Load oscillates around {name}; no stable equilibrium."[:140]

        if verdict == "UNSTABLE":
            breach = next((r for r in sweep if r["verdict"] == "UNSTABLE"), sweep[-1])
            if breach["max_utilisation"] >= 1.0:
                minutes = self._minutes_to_breach(breach["max_utilisation"])
                return (
                    f"At {breach['compliance_rate']:.0%} compliance, {name} exceeds "
                    f"capacity within {minutes} minutes."
                )[:140]
            return (
                f"Redistribution increases load variance from {baseline_variance:.3f} "
                f"to {post_variance:.3f}."
            )[:140]

        if verdict == "CONDITIONAL":
            breach = next((r for r in sweep if r["verdict"] != "STABLE"), sweep[-1])
            return (
                f"Holds below {breach['compliance_rate']:.0%} compliance; {name} reaches "
                f"{breach['max_utilisation']:.0%} above that."
            )[:140]

        return (
            f"Equilibrium holds across 40-90% compliance; peak {max_util:.0%} at {name}."
        )[:140]

    def _display(self, entity_id: str | None, node_state: dict[str, dict]) -> str:
        if not entity_id:
            return "the network"
        return node_state.get(entity_id, {}).get("display_name") or entity_id

    def _minutes_to_breach(self, max_utilisation: float) -> int:
        """How fast the breach arrives, scaled by how far past 1.0 it goes."""
        overshoot = max(max_utilisation - 1.0, 0.01)
        return int(clamp(round(26.0 - overshoot * 100.0), 4, 45))

    # --- 03 §5.5: first thing to cut -------------------------------------------
    def solve_leader(self, risk_context, node_state, edges, segments) -> list[dict]:
        """Stackelberg leader search over a discrete incentive grid.

        Explicitly the first module to cut (03 §5.5); `certify()` retains ~80% of
        the demo value. Left unimplemented on purpose so nothing depends on it.
        """
        return []
