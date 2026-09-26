"""RiskScorer (03_ML_CONTRACT.md §7.1).

Deliberately not machine learning. The operator has to be able to ask "why 72?"
and get an arithmetic answer, which a learned score cannot give. 03 §7.1 is
explicit that this is a considered downgrade — do not "upgrade" it.

Phase 1E — capacity invariant (FINAL_AUDIT_REPORT P0-06). The 03 §7.1 formula
caps the utilisation term at 50 points, so an entity at 100-120% of capacity
with a flat forecast scored 50-60 ("moderate") and 150% scored 75 ("high").
The formula is unchanged; one rule is added on top of it:

    utilisation >= 1.0  =>  score = max(formula, first CRITICAL score = high + 1)

so the band is CRITICAL and the score agrees with it. Below capacity the
formula is untouched, so pre-capacity states stay differentiated. Non-finite
inputs are treated as missing (0) and logged — a NaN reading used to clamp
silently to 100 / critical.
"""
from __future__ import annotations

import logging
import math

from .common import band_from_score, clamp

log = logging.getLogger("eventflow.ml.risk")

# At or above this utilisation an entity is over capacity: always CRITICAL.
CAPACITY_UTILISATION = 1.0


class RiskScorer:
    def __init__(self, config: dict) -> None:
        self.config = config or {}
        w = self.config.get("weights", {})
        self.w_base = float(w.get("base", 0.5))
        self.w_growth = float(w.get("growth", 0.3))
        self.w_cascade = float(w.get("cascade", 0.2))
        self.bands = self.config.get("risk_bands", {"low": 30, "moderate": 60, "high": 80})

    def ready(self) -> bool:
        return True

    def score(
        self,
        node_state: dict[str, dict],
        forecast: dict[str, dict] | None = None,
        cascade_exposure: dict[str, float] | None = None,
    ) -> dict[str, dict]:
        forecast = forecast or {}
        cascade_exposure = cascade_exposure or {}
        try:
            return {
                eid: self._score_one(eid, st, forecast.get(eid), cascade_exposure.get(eid, 0.0))
                for eid, st in node_state.items()
            }
        except Exception:
            log.exception("risk scoring failed; falling back to utilisation-only")
            return self.fallback(node_state, forecast, cascade_exposure)

    def fallback(
        self,
        node_state: dict[str, dict],
        forecast: dict[str, dict] | None = None,
        cascade_exposure: dict[str, float] | None = None,
    ) -> dict[str, dict]:
        out = {}
        for eid, st in node_state.items():
            util = self._finite(eid, "utilisation", st.get("utilisation", 0.0))
            score = int(round(clamp(100.0 * util, 0, 100)))
            if util >= CAPACITY_UTILISATION:
                score = max(score, int(self.bands["high"]) + 1)
            out[eid] = {
                "risk_score": score,
                "risk_band": band_from_score(score, self.bands),
                "breakdown": [{"risk_type": "overall", "score": score}],
            }
        return out

    def _finite(self, entity_id: str, name: str, value, default: float = 0.0) -> float:
        try:
            v = float(value)
        except (TypeError, ValueError):
            v = math.nan
        if not math.isfinite(v):
            log.warning("risk: non-finite %s for %s treated as missing", name, entity_id)
            return default
        return v

    def _score_one(self, entity_id: str, state: dict, forecast: dict | None, exposure: float) -> dict:
        util = self._finite(entity_id, "utilisation", state.get("utilisation", 0.0))
        forecast_1800 = util
        if forecast:
            for p in forecast.get("points", []):
                if p["horizon_sec"] == 1800:
                    forecast_1800 = self._finite(entity_id, "forecast_1800", p["predicted_utilisation"], util)

        base = 100.0 * util
        growth = 100.0 * max(0.0, forecast_1800 - util) * 1.5
        cascade_x = 100.0 * clamp(self._finite(entity_id, "cascade_exposure", exposure), 0.0, 1.0)
        score = int(round(clamp(self.w_base * base + self.w_growth * growth + self.w_cascade * cascade_x, 0, 100)))
        if util >= CAPACITY_UTILISATION:
            # Capacity invariant (Phase 1E): at or over capacity is CRITICAL.
            score = max(score, int(self.bands["high"]) + 1)

        return {
            "risk_score": score,
            "risk_band": band_from_score(score, self.bands),
            "breakdown": self._breakdown(state, base, growth, cascade_x, score),
        }

    def _breakdown(self, state: dict, base: float, growth: float, cascade_x: float, score: int) -> list[dict]:
        """Per-risk-type split. The types shown depend on what the entity actually is."""
        entity_type = state.get("entity_type", "zone")
        primary = {
            "zone": "crowd", "venue": "crowd", "gate": "capacity",
            "transport_node": "transport", "transport_route": "transport",
            "road": "traffic", "hotel": "hospitality", "parking": "parking",
            "emergency_facility": "emergency",
        }.get(entity_type, "crowd")

        rows = [
            {"risk_type": primary, "score": int(round(clamp(base, 0, 100)))},
            {"risk_type": "capacity", "score": int(round(clamp(base + growth * 0.5, 0, 100)))},
            {"risk_type": "cascading", "score": int(round(clamp(cascade_x, 0, 100)))},
            {"risk_type": "overall", "score": score},
        ]
        # Dedupe when `primary` is already "capacity".
        seen, out = set(), []
        for r in rows:
            if r["risk_type"] in seen:
                continue
            seen.add(r["risk_type"])
            out.append(r)
        return out
