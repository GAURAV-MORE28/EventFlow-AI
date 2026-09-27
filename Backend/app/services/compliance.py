"""Attendee compliance, estimated from nudge answers (01 §3.12).

Every accepted / declined nudge is one Bernoulli trial for the answering
attendee's segment. Two Beta-Binomial posteriors are kept over the same answers:

  pooled       prior mean `interventions.default_compliance`: the share of
               visitors the simulator assumes follow an active intervention
               (`generator.set_compliance`) and candidate evaluation runs at.
  per segment  prior mean the segment's own `compliance_base_rate` (00 §2.10):
               the rate the equilibrium solver's followers comply at when it
               certifies a candidate (03 §5.2).

Both priors carry `compliance.prior_strength` pseudo-answers, so a handful of
answers moves an estimate a little and a few hundred dominate it. An answer
without a known segment updates only the pooled estimate.

Pure arithmetic over `store.observed_compliance`; the list is the only state,
so a reset (which clears it) resets every estimate.
"""
from __future__ import annotations

import math
from typing import Any

DEFAULT_PRIOR_STRENGTH = 10.0
Z_90 = 1.6449


def _posterior(prior_mean: float, strength: float, accepted: int, n: int) -> dict[str, Any]:
    a = prior_mean * strength + accepted
    b = (1.0 - prior_mean) * strength + (n - accepted)
    mean = a / (a + b)
    # 90% credible interval, normal approximation to the Beta (ample at a+b >= 10).
    sd = math.sqrt(a * b / ((a + b) ** 2 * (a + b + 1.0)))
    return {
        "mean": round(mean, 4),
        "lower_90": round(max(0.0, mean - Z_90 * sd), 4),
        "upper_90": round(min(1.0, mean + Z_90 * sd), 4),
        "alpha": round(a, 4), "beta": round(b, 4),
        "answers": n, "accepted": accepted,
    }


class ComplianceEstimator:
    def __init__(self, segments: list[dict], pooled_prior: float, prior_strength: float = DEFAULT_PRIOR_STRENGTH) -> None:
        self.segments = segments
        self.pooled_prior = float(pooled_prior)
        self.strength = max(float(prior_strength), 1e-6)

    @staticmethod
    def _counts(answers: list[dict], segment_id: str | None = None) -> tuple[int, int]:
        rows = answers if segment_id is None else [a for a in answers if a.get("segment_id") == segment_id]
        return sum(1 for a in rows if a["accepted"]), len(rows)

    def pooled(self, answers: list[dict]) -> dict[str, Any]:
        accepted, n = self._counts(answers)
        return _posterior(self.pooled_prior, self.strength, accepted, n)

    def by_segment(self, answers: list[dict]) -> dict[str, dict[str, Any]]:
        out = {}
        for s in self.segments:
            accepted, n = self._counts(answers, s["segment_id"])
            out[s["segment_id"]] = {"prior": float(s["compliance_base_rate"]),
                                    **_posterior(float(s["compliance_base_rate"]), self.strength, accepted, n)}
        return out

    def solver_segments(self, answers: list[dict]) -> list[dict]:
        """The segments as the equilibrium solver should see them: the published
        definition with `compliance_base_rate` replaced by its posterior mean."""
        post = self.by_segment(answers)
        return [{**s, "compliance_base_rate": post[s["segment_id"]]["mean"]} for s in self.segments]
