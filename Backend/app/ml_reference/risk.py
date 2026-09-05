"""RiskScorer (03_ML_CONTRACT.md §7.1).

Deliberately not machine learning. The operator has to be able to ask "why 72?"
and get an arithmetic answer, which a learned score cannot give. 03 §7.1 is
explicit that this is a considered downgrade — do not "upgrade" it.
"""
from __future__ import annotations

import logging

from .common import band_from_score, clamp

log = logging.getLogger("eventflow.ml.risk")


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
            score = int(round(clamp(100.0 * float(st.get("utilisation", 0.0)), 0, 100)))
            out[eid] = {
                "risk_score": score,
                "risk_band": band_from_score(score, self.bands),
                "breakdown": [{"risk_type": "overall", "score": score}],
            }
        return out

    def _score_one(self, entity_id: str, state: dict, forecast: dict | None, exposure: float) -> dict:
        util = float(state.get("utilisation", 0.0))
        forecast_1800 = util
        if forecast:
            for p in forecast.get("points", []):
                if p["horizon_sec"] == 1800:
                    forecast_1800 = float(p["predicted_utilisation"])

        base = 100.0 * util
        growth = 100.0 * max(0.0, forecast_1800 - util) * 1.5
        cascade_x = 100.0 * clamp(exposure, 0.0, 1.0)
        score = int(round(clamp(self.w_base * base + self.w_growth * growth + self.w_cascade * cascade_x, 0, 100)))

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
