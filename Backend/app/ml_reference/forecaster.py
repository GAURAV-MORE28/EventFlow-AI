"""Forecaster (03_ML_CONTRACT.md §2).

Reference implementation of the model-selection ladder:

    len(history) < 10                      -> "persistence"
    tsfm_enabled and a TSFM is importable  -> "tsfm"
    otherwise                              -> "local_model"

No Chronos-Bolt weights ship with the backend, so the ladder degrades to
`local_model` (damped linear trend on a rolling window) and *says so* in the
`source` field. That is the contract's degradation rule working as designed —
`ml/forecaster.py` replaces this class and the badge starts reading "tsfm".

`baseline_comparison` is returned even when the model loses to persistence
(03 §2.3, "never suppress an unfavourable improvement_pct").
"""
from __future__ import annotations

import logging
from typing import Any

from .common import clamp

log = logging.getLogger("eventflow.ml.forecaster")

MIN_HISTORY_FOR_MODEL = 10
MIN_HISTORY_FOR_BASELINE = 10


class Forecaster:
    def __init__(self, config: dict) -> None:
        self.config = config or {}
        self.horizons: list[int] = list(self.config.get("horizons_sec", [900, 1800, 3600]))
        self.critical = float(self.config.get("critical_utilisation", 0.90))
        # Per-entity critical line (venues/hotels run near full by design).
        self.critical_by_entity: dict[str, float] = dict(self.config.get("critical_by_entity") or {})
        self.warm_start_after = int(self.config.get("warm_start_after_points", 40))
        self.step_sec = int(self.config.get("step_sec", 30))
        self._tsfm = self._try_load_tsfm() if self.config.get("tsfm_enabled", True) else None

    def _try_load_tsfm(self) -> Any | None:
        try:  # pragma: no cover - only exercised when the ML workstream adds weights
            from chronos import BaseChronosPipeline  # type: ignore

            return BaseChronosPipeline.from_pretrained(
                self.config.get("tsfm_model", "amazon/chronos-bolt-small"),
                device_map=self.config.get("device", "cpu"),
            )
        except Exception:
            log.info("no TSFM available; forecaster ladder degrades to local_model")
            return None

    def ready(self) -> bool:
        return True

    def active_source(self, history_len: int = 0) -> str:
        if history_len < MIN_HISTORY_FOR_MODEL:
            return "persistence"
        if self._tsfm is not None and history_len < self.warm_start_after:
            return "tsfm"
        return "local_model"

    # --- 03 §2.1 primary method -------------------------------------------
    def predict(
        self,
        series: dict[str, list[float]],
        capacities: dict[str, float],
        horizons_sec: list[int] | None = None,
        sim_time: str | None = None,
    ) -> dict[str, dict]:
        horizons = list(horizons_sec or self.horizons)
        try:
            return {
                eid: self._forecast_one(eid, hist, horizons, sim_time)
                for eid, hist in series.items()
            }
        except Exception:  # 03 §0 rule 6 — no exception escapes
            log.exception("forecast failed; using persistence fallback")
            return self.fallback(series, capacities, horizons, sim_time)

    def fallback(
        self,
        series: dict[str, list[float]],
        capacities: dict[str, float] | None = None,
        horizons_sec: list[int] | None = None,
        sim_time: str | None = None,
    ) -> dict[str, dict]:
        """Persistence: the last observed value at every horizon."""
        horizons = list(horizons_sec or self.horizons)
        out: dict[str, dict] = {}
        for eid, hist in series.items():
            last = float(hist[-1]) if hist else 0.0
            points = [
                {
                    "horizon_sec": h,
                    "predicted_utilisation": round(last, 4),
                    "lower_90": round(max(0.0, last - 0.08), 4),
                    "upper_90": round(last + 0.08, 4),
                }
                for h in horizons
            ]
            out[eid] = {
                "source": "persistence",
                "generated_at": sim_time,
                "baseline_value": round(last, 4),
                "points": points,
                "time_to_critical_sec": self._time_to_critical(last, points, eid),
                "baseline_comparison": None,
            }
        return out

    # --- internals ---------------------------------------------------------
    def _forecast_one(
        self, entity_id: str, history: list[float], horizons: list[int], sim_time: str | None
    ) -> dict:
        last = float(history[-1]) if history else 0.0
        source = self.active_source(len(history))

        if source == "persistence":
            predicted = {h: last for h in horizons}
            spread = 0.08
        else:
            slope, accel, spread = self._trend(history)
            predicted = {}
            for h in horizons:
                steps = h / self.step_sec
                # A pre-event surge is *convex* — a straight line fitted to it
                # under-predicts the crossing badly, which would push
                # time_to_critical_sec well outside its 180s error target. So the
                # local model carries a curvature term, and damps only the far
                # horizon, where a surge really does flatten against capacity.
                damping = 1.0 / (1.0 + 0.004 * max(0.0, steps - 20.0))
                curve = 0.5 * accel * steps * steps
                # Curvature is the least reliable term; cap its contribution so a
                # noisy second difference cannot manufacture a crisis.
                curve = clamp(curve, -0.35, 0.35)
                # Damping applies to the extrapolated *increment*, never to the
                # observed level — scaling `last` down would make a busy station
                # look emptier the further ahead you look.
                predicted[h] = clamp(last + (slope * steps + curve) * damping, 0.0, 1.6)

        points = []
        for h in horizons:
            value = predicted[h]
            widen = spread * (1.0 + h / 3600.0)
            points.append(
                {
                    "horizon_sec": h,
                    "predicted_utilisation": round(value, 4),
                    "lower_90": round(max(0.0, value - widen), 4),
                    "upper_90": round(value + widen, 4),
                }
            )

        return {
            "source": source,
            "generated_at": sim_time,
            "baseline_value": round(last, 4),
            "points": points,
            "time_to_critical_sec": self._time_to_critical(last, points, entity_id),
            "baseline_comparison": self._baseline_comparison(history),
        }

    @staticmethod
    def _slope(window: list[float]) -> float:
        """Least-squares slope per step."""
        n = len(window)
        if n < 2:
            return 0.0
        mean_x = (n - 1) / 2.0
        mean_y = sum(window) / n
        denom = sum((i - mean_x) ** 2 for i in range(n))
        if denom == 0:
            return 0.0
        return sum((i - mean_x) * (window[i] - mean_y) for i in range(n)) / denom

    # Curvature is the noisiest term in the fit, so it is shrunk toward zero
    # (ridge-style). Without this a couple of noisy samples manufacture an
    # acceleration and the hero number jumps around between cycles.
    CURVATURE_SHRINKAGE = 0.35

    def _trend(self, history: list[float]) -> tuple[float, float, float]:
        """Slope, curvature and a ~90% residual spread from a quadratic fit.

        Fitted over the whole window rather than by differencing halves: least
        squares is far steadier under sensor noise, and the crossing estimate is
        what the demo hangs on.
        """
        window = history[-16:]
        n = len(window)
        if n < 2:
            return 0.0, 0.0, 0.08
        if n < 6:
            return self._slope(window), 0.0, 0.08

        # Centre x so the normal equations stay well conditioned.
        mean_x = (n - 1) / 2.0
        xs = [i - mean_x for i in range(n)]
        s0, s2, s4 = float(n), sum(x * x for x in xs), sum(x ** 4 for x in xs)
        t0 = sum(window)
        t1 = sum(x * y for x, y in zip(xs, window))
        t2 = sum(x * x * y for x, y in zip(xs, window))

        # Odd moments vanish for a symmetric grid, so a/c decouple from b.
        det = s0 * s4 - s2 * s2
        if abs(det) < 1e-12:
            return self._slope(window), 0.0, 0.08
        quad = (s0 * t2 - s2 * t0) / det          # coefficient of x^2
        const = (s4 * t0 - s2 * t2) / det
        lin = t1 / s2 if s2 else 0.0              # coefficient of x

        quad *= self.CURVATURE_SHRINKAGE

        # Re-express around the last observation, which is where extrapolation starts.
        end = xs[-1]
        slope = lin + 2.0 * quad * end
        accel = 2.0 * quad

        resid = [
            window[i] - (const + lin * xs[i] + quad * xs[i] * xs[i]) for i in range(n)
        ]
        spread = 1.645 * (sum(r * r for r in resid) / n) ** 0.5
        return slope, accel, max(spread, 0.015)

    def _time_to_critical(self, current: float, points: list[dict], entity_id: str | None = None) -> int | None:
        """03 §2.4 — linear interpolation to the first crossing of the critical threshold.

        This field matters more than MAE: it is the number on the screen.
        """
        critical = self.critical_by_entity.get(entity_id or "", self.critical)
        if current >= critical:
            return 0
        prev_h, prev_v = 0, current
        for p in points:
            h, v = p["horizon_sec"], p["predicted_utilisation"]
            if v >= critical:
                if v == prev_v:
                    return int(h)
                frac = (critical - prev_v) / (v - prev_v)
                return int(round(prev_h + frac * (h - prev_h)))
            prev_h, prev_v = h, v
        return None

    def _baseline_comparison(self, history: list[float]) -> dict | None:
        """Rolling walk-forward MAE at one step, model vs persistence. Reported honestly."""
        if len(history) < MIN_HISTORY_FOR_BASELINE:
            return None
        window = history[-30:]
        pers_err, model_err, n = 0.0, 0.0, 0
        for i in range(5, len(window) - 1):
            actual = window[i + 1]
            pers_err += abs(actual - window[i])
            slope, accel, _ = self._trend(window[: i + 1])
            model_err += abs(actual - (window[i] + slope + 0.5 * accel))
            n += 1
        if n == 0:
            return None
        persistence_mae = pers_err / n
        model_mae = model_err / n
        improvement = 0.0 if persistence_mae == 0 else (persistence_mae - model_mae) / persistence_mae * 100.0
        return {
            "persistence_mae": round(persistence_mae, 4),
            "model_mae": round(model_mae, 4),
            "improvement_pct": round(improvement, 1),
        }
