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
    """Severity = a floor set by current utilisation, escalated by what is coming.

    The floor maps utilisation onto the band scale so a capacity breach can never
    read as harmless: at/above `critical_utilisation` the score is at least 81
    (critical); at/above `warning_utilisation` it is at least 61 (high). A
    weighted blend used to cap current utilisation's contribution at 50 points,
    which reported a zone at 104% of capacity as "moderate".

    Escalations are additive and bounded, each explainable in one sentence:
      growth      — the 30-minute forecast is above today's level
      cascading   — the entity sits on a predicted failure path
      persistence — it has been over the critical line for consecutive cycles
    """

    def __init__(self, config: dict) -> None:
        self.config = config or {}
        self.bands = self.config.get("risk_bands", {"low": 30, "moderate": 60, "high": 80})
        self.critical = float(self.config.get("critical_utilisation", 0.90))
        self.warning = float(self.config.get("warning_utilisation", 0.75))
        self.max_growth = float(self.config.get("max_growth_points", 15))
        self.max_cascade = float(self.config.get("max_cascade_points", 10))
        self.max_persist = float(self.config.get("max_persistence_points", 5))
        self.persist_cycles = max(1, int(self.config.get("persistence_cycles_for_max", 20)))
        self.by_type: dict[str, dict] = dict(self.config.get("thresholds_by_type") or {})

    def _lines(self, entity_type: str | None) -> tuple[float, float]:
        t = self.by_type.get(entity_type or "", {})
        return float(t.get("warning", self.warning)), float(t.get("critical", self.critical))

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
            score = int(round(self.utilisation_floor(float(st.get("utilisation", 0.0)), st.get("entity_type"))))
            out[eid] = {
                "risk_score": score,
                "risk_band": band_from_score(score, self.bands),
                "breakdown": [{"risk_type": "overall", "score": score}],
            }
        return out

    def utilisation_floor(self, util: float, entity_type: str | None = None) -> float:
        """Piecewise-linear map of utilisation onto the 0-100 band scale.

        < warning/1.5 ... low, up to warning ... moderate, up to critical ...
        high, at/above critical ... critical (81+, reaching 100 at +30pp).
        """
        b = self.bands
        warning, critical = self._lines(entity_type)
        low_top = warning / 1.5
        if util < low_top:
            return clamp(util / low_top * b["low"], 0.0, b["low"])
        if util < warning:
            return b["low"] + (util - low_top) / (warning - low_top) * (b["moderate"] - b["low"])
        if util < critical:
            return b["moderate"] + 1 + (util - warning) / (critical - warning) * (b["high"] - b["moderate"] - 1)
        return clamp(b["high"] + 1 + (util - critical) / 0.30 * (99 - b["high"]), b["high"] + 1, 100.0)

    def _score_one(self, entity_id: str, state: dict, forecast: dict | None, exposure: float) -> dict:
        util = float(state.get("utilisation", 0.0))
        forecast_1800 = util
        if forecast:
            for p in forecast.get("points", []):
                if p["horizon_sec"] == 1800:
                    forecast_1800 = float(p["predicted_utilisation"])
        elif state.get("forecast_1800") is not None:
            forecast_1800 = float(state["forecast_1800"])

        etype = state.get("entity_type")
        warning, critical = self._lines(etype)
        base = self.utilisation_floor(util, etype)
        # Growth counts only when the forecast climbs toward/over the line.
        growth = clamp((forecast_1800 - util) / 0.20, 0.0, 1.0) * self.max_growth if forecast_1800 > warning * 0.9 else 0.0
        # Escalations can raise severity, but never manufacture a critical
        # band for an entity still below its own warning line.
        cap = 100.0 if util >= warning else float(self.bands["high"])
        cascade_pts = clamp(exposure, 0.0, 1.0) * self.max_cascade if util >= warning * 0.8 else 0.0
        cycles_over = int(state.get("cycles_over_critical", 0) or 0)
        persist = min(1.0, cycles_over / self.persist_cycles) * self.max_persist if util >= critical else 0.0
        score = int(round(clamp(min(base + growth + cascade_pts + persist, max(cap, base)), 0, 100)))

        return {
            "risk_score": score,
            "risk_band": band_from_score(score, self.bands),
            "breakdown": self._breakdown(state, base, growth, cascade_pts, persist, score),
        }

    def _breakdown(self, state: dict, base: float, growth: float, cascade_pts: float, persist: float, score: int) -> list[dict]:
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
            {"risk_type": "capacity", "score": int(round(clamp(base + growth, 0, 100)))},
            {"risk_type": "cascading", "score": int(round(clamp(cascade_pts / max(self.max_cascade, 1) * 100, 0, 100)))},
            {"risk_type": "overall", "score": score},
        ]
        seen, out = set(), []
        for r in rows:
            if r["risk_type"] in seen:
                continue
            seen.add(r["risk_type"])
            out.append(r)
        return out
