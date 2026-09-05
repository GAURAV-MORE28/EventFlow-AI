"""AnomalyDetector (03_ML_CONTRACT.md §7.2).

Rolling z-score on forecast residuals. Deliberately not machine learning: it is
explainable, fast, and sufficient. Target is a <10% false-positive rate at equal
recall against a fixed threshold.
"""
from __future__ import annotations

import logging

log = logging.getLogger("eventflow.ml.anomaly")


class AnomalyDetector:
    """Rolling z-score on forecast residuals. Explainable and fast (03 §7.2)."""

    def __init__(self, config: dict) -> None:
        self.config = config or {}
        self.z_threshold = float(self.config.get("z_threshold", 3.0))
        self.window = int(self.config.get("window", 20))

    def ready(self) -> bool:
        return True

    def detect(self, residuals: dict[str, list[float]], z_threshold: float | None = None) -> list[dict]:
        threshold = float(z_threshold if z_threshold is not None else self.z_threshold)
        try:
            out: list[dict] = []
            for eid, series in residuals.items():
                window = series[-self.window:]
                if len(window) < 5:
                    continue
                mean = sum(window) / len(window)
                var = sum((v - mean) ** 2 for v in window) / len(window)
                sd = var ** 0.5
                if sd < 1e-6:
                    continue
                z = (window[-1] - mean) / sd
                if abs(z) >= threshold:
                    direction = "above" if z > 0 else "below"
                    out.append(
                        {
                            "entity_id": eid,
                            "z_score": round(z, 2),
                            "description": (
                                f"Observed load is {abs(z):.1f} standard deviations {direction} "
                                f"the recent forecast residual for {eid}."
                            ),
                        }
                    )
            return sorted(out, key=lambda r: -abs(r["z_score"]))
        except Exception:
            log.exception("anomaly detection failed")
            return self.fallback(residuals, threshold)

    def fallback(self, residuals: dict[str, list[float]], z_threshold: float | None = None) -> list[dict]:
        return []
