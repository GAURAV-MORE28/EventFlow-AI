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
(03 §2.3, "never suppress an unfavourable improvement_pct"). It scores the
model that produced the forecast: each forecast's 900s point is compared with
what was actually observed 900s later, against persistence over the same
interval, per entity, over forecasts from the same `source`.
"""
from __future__ import annotations

import logging
import threading
from collections import deque
from datetime import datetime
from typing import Any

import math

from .common import clamp

log = logging.getLogger("eventflow.ml.forecaster")

MIN_HISTORY_FOR_MODEL = 10
MIN_SCORED_FOR_BASELINE = 10   # validated forecasts needed before a comparison is reported
BASELINE_WINDOW = 30           # most recent validated forecasts per entity
VALIDATION_HORIZON_SEC = 900


def _epoch(sim_time: str | None) -> float | None:
    if not sim_time:
        return None
    try:
        return datetime.fromisoformat(sim_time.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


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
        # Honest baseline_comparison: forecasts awaiting their outcome and the
        # scored ones. The lock matters because a predict() that overran its
        # budget still finishes in its worker thread.
        self._validation_h = VALIDATION_HORIZON_SEC if VALIDATION_HORIZON_SEC in self.horizons else min(self.horizons)
        self._pending: dict[str, deque[tuple[float, str, float, float]]] = {}
        self._scored: dict[str, deque[tuple[str, float, float]]] = {}
        self._last_t: float | None = None
        self._lock = threading.Lock()

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
        model_prior: dict[str, dict] | None = None,
    ) -> dict[str, dict]:
        """`model_prior` (optional): per entity `{"now": u, "points": {h: u}}`, the
        digital twin's process-model projection under the announced plan. When
        given, the forecast is that projection corrected by the gap between the
        latest reading and the model's own "now" (the correction decays with
        horizon: an unexplained deviation is assumed to fade, not to persist
        forever). Without it the local trend model is used."""
        horizons = list(horizons_sec or self.horizons)
        try:
            t = _epoch(sim_time)
            with self._lock:
                self._score_pending(series, t)
                out = {}
                for eid, hist in series.items():
                    prior = (model_prior or {}).get(eid)
                    if prior and len(hist) >= 2:
                        out[eid] = self._forecast_model(eid, hist, prior, horizons, sim_time)
                    else:
                        out[eid] = self._forecast_one(eid, hist, horizons, sim_time)
                self._record(out, t)
            return out
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
    BIAS_DECAY_SEC = 1800.0

    def _forecast_model(self, entity_id: str, history: list[float], prior: dict,
                        horizons: list[int], sim_time: str | None) -> dict:
        last = float(history[-1])
        gap = last - float(prior.get("now", last))
        # Spread from how well the model tracked this entity recently (the gap
        # itself), floored so a perfect run still carries honest uncertainty.
        spread = max(0.02, abs(gap) * 0.5)
        points = []
        for h in horizons:
            base = float(prior["points"].get(h, prior["points"].get(str(h), last)))
            value = clamp(base + gap * math.exp(-h / self.BIAS_DECAY_SEC), 0.0, 1.8)
            widen = spread * (1.0 + h / 3600.0)
            points.append({
                "horizon_sec": h,
                "predicted_utilisation": round(value, 4),
                "lower_90": round(max(0.0, value - widen), 4),
                "upper_90": round(value + widen, 4),
            })
        return {
            "source": "twin_model",
            "generated_at": sim_time,
            "baseline_value": round(last, 4),
            "points": points,
            "time_to_critical_sec": self._time_to_critical(last, points, entity_id),
            "baseline_comparison": self._baseline_comparison(entity_id, "twin_model"),
        }

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
            "baseline_comparison": self._baseline_comparison(entity_id, source),
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

    # --- baseline_comparison: the producing model, validated at its horizon -------
    def _score_pending(self, series: dict[str, list[float]], t: float | None) -> None:
        """Score every forecast made `_validation_h` ago against the latest reading."""
        if t is None:
            return
        if self._last_t is not None and t < self._last_t:
            # The clock moved backwards (seek / reset): old forecasts are not comparable.
            self._pending.clear()
            self._scored.clear()
        self._last_t = t
        h = self._validation_h
        for eid, pending in self._pending.items():
            hist = series.get(eid)
            if not hist:
                continue
            actual = float(hist[-1])
            while pending and pending[0][0] <= t - h:
                t0, source, predicted, last_then = pending.popleft()
                # Only an outcome observed at (about) the forecast's own horizon
                # counts; a skipped refresh must not stretch the horizon.
                if t - t0 <= h + 2 * self.step_sec:
                    self._scored.setdefault(eid, deque(maxlen=BASELINE_WINDOW)).append(
                        (source, abs(actual - predicted), abs(actual - last_then)))

    def _record(self, forecasts: dict[str, dict], t: float | None) -> None:
        if t is None:
            return
        h = self._validation_h
        for eid, fc in forecasts.items():
            predicted = next((p["predicted_utilisation"] for p in fc["points"] if p["horizon_sec"] == h), None)
            if predicted is None:
                continue
            pending = self._pending.setdefault(eid, deque(maxlen=h // max(self.step_sec, 1) + 10))
            if pending and pending[-1][0] == t:
                pending.pop()   # the same instant re-stated (operator change): keep the latest forecast
            pending.append((t, fc["source"], float(predicted), float(fc["baseline_value"])))

    def _baseline_comparison(self, entity_id: str, source: str) -> dict | None:
        """MAE of this entity's `source` forecasts at the validation horizon vs
        persistence over the same interval. Reported honestly, also when it loses."""
        rows = [r for r in self._scored.get(entity_id, ()) if r[0] == source]
        if len(rows) < MIN_SCORED_FOR_BASELINE:
            return None
        model_mae = sum(r[1] for r in rows) / len(rows)
        persistence_mae = sum(r[2] for r in rows) / len(rows)
        improvement = 0.0 if persistence_mae == 0 else (persistence_mae - model_mae) / persistence_mae * 100.0
        return {
            "persistence_mae": round(persistence_mae, 4),
            "model_mae": round(model_mae, 4),
            "improvement_pct": round(improvement, 1),
        }
