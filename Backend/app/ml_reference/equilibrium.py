"""EquilibriumSolver (03_ML_CONTRACT.md §5) — the signature innovation.

A Stackelberg game: the organiser sets incentives, attendee segments best-respond
by minimising personal cost. `certify()` runs the follower equilibrium at three
compliance rates and reports whether the intervention actually holds.

`derive_verdict` and `row_verdict` are defined **here and nowhere else**
(03 §5.3). The backend renders the verdict; the frontend renders the verdict;
neither recomputes it. If you find a second copy of this arithmetic, delete it.

`reason` is template-generated and deterministic — never LLM-written (03 §9).

Phase 1D rewrite of everything that FEEDS the verdict table (the table itself is
unchanged). FINAL_AUDIT_REPORT P0-05 measured: the old follower loop removed
the intervention's relief from the targets again on EVERY iteration, so any
relief >= 8% hit the 40-iteration cap and was UNSTABLE by construction; the
surface included every zone, so one unrelated overloaded zone made any action
UNSTABLE; and "exceeds capacity within N minutes" was 26 - overshoot x 100.

Now:

FOLLOWER RESPONSE — a genuine fixed point. An action OFFERS a move of
`planned_fraction` of each source's people (Intervention.action_effects).
m in [0, 1] is the share of that offer attendees take; it must satisfy

    m = F(m) = sum_s share_s * base_rate_s * rate * sigmoid(k * (gap_s(m) + incentive_s))

    gap_s(m)       = cost(source after moving m) - cost(destination after receiving m)
                     (deferral: cost of waiting = time_elasticity_s * duration / 3600)
    cost(u)        = alpha * u ** beta                      (03 §5.2 congestion term)
    incentive_s    = relief * price_elasticity_s             (as before)

As m grows the source empties and the receiver fills, so F falls: the iteration
m <- (1 - damping) m + damping F(m) converges to where the marginal attendee is
indifferent. Relief is never re-applied. Convergence: |dm| < convergence_tol
within max_iterations; oscillation per 03 §5.2 (t ~ t-2, not ~ t-1, twice);
divergence = non-finite m.

SCOPE — only the entities the action touches (its sources and destinations),
evaluated in the SAME event-world model the simulation runs, through a
`rollout` callable supplied by the backend (this module still imports nothing
from the backend). Per compliance row the moved people are rolled forward over
`horizon_sec` against the matched do-nothing rollout.

ROW INPUTS to row_verdict (03 §5.3):
    max_utilisation   peak utilisation among entities the action ADDS load to
                      ("new critical entity created"); 0 if it adds none
    post/baseline_var variance of the affected entities' peaks with / without

TIME TO BREACH — the first rollout step at which a loaded entity reaches 1.0
(seconds from now), or no minutes in the reason at all. Never a formula.
"""
from __future__ import annotations

import logging
import math
from typing import Any, Callable

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


Rollout = Callable[[list[dict], list[str], int], dict[str, list[float]]]


class EquilibriumSolver:
    HORIZON_SEC = 1800
    STEP_SEC = 30

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
        rollout: Rollout | None = None,
    ) -> dict:
        try:
            return self._certify(intervention, node_state, segments, rollout)
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
            "response_rate": None,
        }

    # --- internals --------------------------------------------------------------
    def _certificate_id(self, intervention_id: str) -> str:
        return "cert_" + f"{int(stable_unit(self.seed, 'cert', intervention_id) * 0xFFFF):04x}"

    @staticmethod
    def _effects(intervention: dict) -> list[dict]:
        return [
            e for e in intervention.get("action_effects") or []
            if e.get("source_entity_id") and float(e.get("planned_fraction", 0.0)) > 0
        ]

    def _certify(self, intervention: dict, node_state: dict[str, dict], segments: list[dict],
                 rollout: Rollout | None) -> dict:
        iid = intervention.get("intervention_id", "int_unknown")
        relief = float(intervention.get("estimated_relief_pct", 0.0)) / 100.0
        effects = [e for e in self._effects(intervention) if e["source_entity_id"] in node_state
                   and (e.get("destination_entity_id") is None or e["destination_entity_id"] in node_state)]
        segments = segments or [{"share": 1.0, "price_elasticity": 0.5, "time_elasticity": 0.5,
                                 "compliance_base_rate": 0.5}]

        affected = list(dict.fromkeys(
            [e["source_entity_id"] for e in effects]
            + [e["destination_entity_id"] for e in effects if e.get("destination_entity_id")]
        ))
        if not effects:
            root = intervention.get("triggered_by_entity_id") or (intervention.get("target_entity_ids") or [None])[0]
            affected = [root] if root in node_state else []

        horizon = self.HORIZON_SEC
        do_nothing = self._project(rollout, [], affected, node_state, horizon)
        dn_peak = {e: max(v) for e, v in do_nothing.items()}

        sweep: list[dict] = []
        converged_all, oscillation_any, iterations_used = True, False, 0
        worst: tuple[float, str | None] = (0.0, None)
        post_var_at_60, baseline_var = 0.0, variance([dn_peak[e] for e in affected])
        breach: tuple[str | None, int | None] = (None, None)
        response_at_60: float | None = None
        for rate in self.sweep_rates:
            m, converged, iters, oscillated = self._solve_response(effects, node_state, segments, relief, rate)
            converged_all = converged_all and converged
            oscillation_any = oscillation_any or oscillated
            iterations_used = max(iterations_used, iters)
            if abs(rate - 0.6) < 1e-9:
                response_at_60 = round(m, 4) if converged else None

            moved = [{**e, "fraction": float(e["planned_fraction"]) * m} for e in effects]
            post = self._project(rollout, moved, affected, node_state, horizon)
            post_peak = {e: max(v) for e, v in post.items()}
            loaded = [e for e in affected if post_peak[e] > dn_peak[e] + 1e-9]
            max_entity = max(loaded, key=lambda e: post_peak[e]) if loaded else None
            max_util = post_peak[max_entity] if max_entity else 0.0
            post_var = variance([post_peak[e] for e in affected])
            if abs(rate - 0.6) < 1e-9:
                post_var_at_60 = post_var
            top = max(affected, key=lambda e: post_peak[e]) if affected else None
            if top and post_peak[top] > worst[0]:
                worst = (post_peak[top], top)
            if max_entity and max_util >= 1.0 and breach[0] is None:
                step = next(i for i, u in enumerate(post[max_entity]) if u >= 1.0)
                breach = (max_entity, (step + 1) * self.STEP_SEC if rollout else 0)

            sweep.append({
                "compliance_rate": rate,
                "max_utilisation": round(max_util, 4),
                "verdict": row_verdict(max_util, post_var, baseline_var),
            })

        verdict = derive_verdict(sweep, converged_all, oscillation_any)
        utils = [row["max_utilisation"] for row in sweep]
        return {
            "certificate_id": self._certificate_id(iid),
            "intervention_id": iid,
            "verdict": verdict,
            "converged": converged_all,
            "iterations": iterations_used,
            "post_nudge_variance": round(post_var_at_60, 4),
            "baseline_variance": round(baseline_var, 4),
            "max_zone_utilisation": round(worst[0], 4),
            "max_zone_entity_id": worst[1],
            "oscillation_risk": oscillation_any,
            "compliance_sensitivity": round(max(utils) - min(utils), 4) if utils else 0.0,
            "reason": self._reason(verdict, sweep, converged_all, oscillation_any, worst,
                                   baseline_var, post_var_at_60, node_state, breach),
            "compliance_sweep": sweep,
            "response_rate": response_at_60,
        }

    # --- the world model ----------------------------------------------------------------
    def _project(self, rollout: Rollout | None, moved: list[dict], affected: list[str],
                 node_state: dict[str, dict], horizon: int) -> dict[str, list[float]]:
        """Per affected entity, utilisation over the horizon with `moved` applied.

        With a rollout (the backend's event-world model) this is the real
        trajectory. Without one — a unit test, or a caller with no world —
        it is the current snapshot with the moved people applied instantly.
        """
        if rollout is not None:
            return rollout(moved, affected, horizon)
        util = {e: float(node_state[e].get("utilisation", 0.0)) for e in affected}
        out = dict(util)
        for eff in moved:
            src, dst, f = eff["source_entity_id"], eff.get("destination_entity_id"), eff["fraction"]
            people = util[src] * float(node_state[src].get("nominal_capacity", 1.0)) * f
            out[src] -= util[src] * f
            if dst:
                out[dst] += people / max(float(node_state[dst].get("nominal_capacity", 1.0)), 1.0)
        return {e: [max(0.0, v)] for e, v in out.items()}

    # --- follower fixed point --------------------------------------------------------------
    def _cost(self, util: float) -> float:
        return self.alpha * (max(util, 0.0) ** self.beta)

    def _post_utils(self, effects: list[dict], node_state: dict[str, dict], m: float) -> dict[str, float]:
        util = {e["source_entity_id"]: float(node_state[e["source_entity_id"]].get("utilisation", 0.0)) for e in effects}
        for e in effects:
            if e.get("destination_entity_id"):
                d = e["destination_entity_id"]
                util.setdefault(d, float(node_state[d].get("utilisation", 0.0)))
        out = dict(util)
        for e in effects:
            src, dst = e["source_entity_id"], e.get("destination_entity_id")
            f = float(e["planned_fraction"]) * m
            out[src] -= util[src] * f
            if dst:
                cap_s = float(node_state[src].get("nominal_capacity", 1.0))
                cap_d = max(float(node_state[dst].get("nominal_capacity", 1.0)), 1.0)
                out[dst] += util[src] * f * cap_s / cap_d
        return out

    def _aggregate_compliance(self, m: float, effects: list[dict], node_state: dict[str, dict],
                              segments: list[dict], relief: float, rate: float) -> float:
        post = self._post_utils(effects, node_state, m)
        total = 0.0
        for seg in segments:
            gaps = []
            for e in effects:
                src, dst = e["source_entity_id"], e.get("destination_entity_id")
                stay = self._cost(post[src])
                if dst:
                    move = self._cost(post[dst])
                else:  # deferral: the cost of the move is the wait
                    move = float(seg.get("time_elasticity", 0.5)) * float(e.get("duration_sec") or 0) / 3600.0
                gaps.append(stay - move)
            gap = sum(gaps) / len(gaps)
            incentive = relief * float(seg.get("price_elasticity", 0.0))
            compliance = float(seg.get("compliance_base_rate", 0.5)) * rate * self._sigmoid(self.sigmoid_k * (gap + incentive))
            total += float(seg.get("share", 0.0)) * compliance
        return total

    def _solve_response(self, effects: list[dict], node_state: dict[str, dict], segments: list[dict],
                        relief: float, rate: float) -> tuple[float, bool, int, bool]:
        """Damped fixed-point iteration for m = F(m). Returns (m, converged, iters, oscillated)."""
        if not effects:
            return 0.0, True, 0, False
        m, history = 0.0, [0.0]
        for it in range(1, self.max_iterations + 1):
            target = self._aggregate_compliance(m, effects, node_state, segments, relief, rate)
            m_new = (1.0 - self.damping) * m + self.damping * target
            if not math.isfinite(m_new):
                return m, False, it, False          # divergence
            m_new = clamp(m_new, 0.0, 1.0)
            history.append(m_new)
            if abs(m_new - m) < self.tol:
                return m_new, True, it, False
            if self._oscillating(history):
                return m_new, False, it, True
            m = m_new
        return m, False, self.max_iterations, False

    def _sigmoid(self, x: float) -> float:
        if x < -60:
            return 0.0
        if x > 60:
            return 1.0
        return 1.0 / (1.0 + math.exp(-x))

    def _oscillating(self, history: list[float]) -> bool:
        """03 §5.2: value at t within tol of t-2 but not of t-1, twice consecutively."""
        if len(history) < 5:
            return False
        def close(a: float, b: float) -> bool:
            return abs(a - b) < self.tol
        checks = []
        for i in (len(history) - 1, len(history) - 2):
            checks.append(close(history[i], history[i - 2]) and not close(history[i], history[i - 1]))
        return all(checks)

    # --- 03 §5.4 reason templates (<=140 chars, deterministic) ------------------
    def _reason(self, verdict: str, sweep: list[dict], converged: bool, oscillation: bool,
                worst: tuple[float, str | None], baseline_variance: float, post_variance: float,
                node_state: dict[str, dict], breach: tuple[str | None, int | None]) -> str:
        max_util, entity = worst
        if oscillation:
            return f"Load oscillates around {self._display(entity, node_state)}; no stable equilibrium."[:140]
        if not converged:
            return "Did not converge within iteration cap."
        if verdict == "UNSTABLE":
            row = next((r for r in sweep if r["verdict"] == "UNSTABLE"), sweep[-1])
            if row["max_utilisation"] >= 1.0 and breach[0]:
                name = self._display(breach[0], node_state)
                if breach[1]:
                    minutes = max(1, round(breach[1] / 60))
                    return (f"At {row['compliance_rate']:.0%} compliance, {name} exceeds capacity "
                            f"within {minutes} minutes.")[:140]
                return f"At {row['compliance_rate']:.0%} compliance, {name} exceeds capacity."[:140]
            # 4 decimals: at 3, a genuine 0.0016 -> 0.0025 rise printed as "0.002 to 0.002".
            return (f"Redistribution increases load variance from {baseline_variance:.4f} "
                    f"to {post_variance:.4f}.")[:140]
        if verdict == "CONDITIONAL":
            row = next((r for r in sweep if r["verdict"] != "STABLE"), sweep[-1])
            return (f"Holds below {row['compliance_rate']:.0%} compliance; "
                    f"{self._display(entity, node_state)} reaches {row['max_utilisation']:.0%} above that.")[:140]
        return f"Equilibrium holds across 40-90% compliance; peak {max_util:.0%} at {self._display(entity, node_state)}."[:140]

    def _display(self, entity_id: str | None, node_state: dict[str, dict]) -> str:
        if not entity_id:
            return "the network"
        return node_state.get(entity_id, {}).get("display_name") or entity_id

    # --- 03 §5.5 leader ------------------------------------------------------------------------
    LEADER_GRID = (0.25, 0.5, 0.75, 1.0)

    def solve_leader(self, risk_context: dict, node_state: dict[str, dict], edges: list[dict],
                     segments: list[dict], rollout: Rollout | None = None) -> list[dict]:
        """Stackelberg leader over a discrete grid of how much diversion to OFFER.

        `risk_context["intervention"]` is a candidate with action_effects. For
        each scale in LEADER_GRID the offer is planned_fraction x scale; the
        followers' response and the certificate are recomputed for that offer.
        Returned best-first: most stable verdict, then largest expected source
        relief (planned x scale x response). Deterministic; [] when the
        candidate moves nobody.
        """
        base = risk_context.get("intervention") or {}
        if not self._effects(base):
            return []
        order = {"STABLE": 0, "CONDITIONAL": 1, "UNSTABLE": 2}
        options = []
        for scale in self.LEADER_GRID:
            offer = {
                **base,
                "estimated_relief_pct": float(base.get("estimated_relief_pct", 0.0)) * scale,
                "action_effects": [
                    {**e, "planned_fraction": round(float(e["planned_fraction"]) * scale, 4)}
                    for e in base.get("action_effects") or []
                ],
            }
            cert = self.certify(offer, node_state, edges, segments, rollout)
            response = cert.get("response_rate") or 0.0
            planned = max((float(e["planned_fraction"]) for e in offer["action_effects"]), default=0.0)
            options.append({
                "scale": scale,
                "planned_fraction": round(planned, 4),
                "response_rate": response,
                "expected_source_relief_pct": round(planned * response * 100.0, 2),
                "verdict": cert["verdict"],
                "max_utilisation": cert["max_zone_utilisation"],
                "reason": cert["reason"],
            })
        options.sort(key=lambda o: (order[o["verdict"]], -o["expected_source_relief_pct"], o["scale"]))
        return options
