"""Forecast correction: LightGBM quantile models on top of the twin-model forecast.

The twin-model forecast (03 §2.2 `model_prior`) is the digital twin's plan
projection plus a decaying live-gap correction. What it cannot know is how the
live city is drifting from the plan right now: a surge running faster than
announced, a queue that is not draining. This module learns that residual

    y_h = actual utilisation at t+h  -  twin-model forecast at h

per horizon h, as three quantiles (5 / 50 / 95 %), from simulated runs logged
through the real engine (`Backend/scripts/forecast_dataset.py`), and trained by
`Backend/scripts/train_forecast_correction.py`. The corrected forecast is
twin + q50, with twin + q05 / twin + q95 as its 90 % band.

`features()` is the only definition of the inputs: the dataset logger and the
serving forecaster both call it on exactly what the forecaster's `predict()`
receives (series, capacity, the entity's critical line, `model_prior`) plus the
twin-model points the forecaster computed from them.

Bundle (ML/artifacts/<version>/, bound by manifest.json, see ML/manifest.py):
    features.json            {"feature_names": [...], "horizons_sec": [...], "quantiles": [...]}
    h<H>_q<QQ>.txt.gz        one LightGBM booster per horizon and quantile (gzip, mtime 0)
    eval.json                held-out evaluation, including the ship gate
A bundle whose files do not verify, whose feature layout differs from
FEATURE_NAMES, or whose eval.json gate did not pass is not loaded.

Plain lists / NumPy in and out; nothing backend-shaped (03 §0).
"""
from __future__ import annotations

import gzip
import json
import math
from pathlib import Path
from typing import Any

import numpy as np

HORIZONS = (900, 1800, 3600)
QUANTILES = (0.05, 0.5, 0.95)
LAGS = (1, 2, 5, 10, 20, 40)      # cycles back
FEATURE_NAMES: tuple[str, ...] = (
    "last", "prior_now", "gap",
    *(f"twin_{h}" for h in HORIZONS),
    *(f"plan_delta_{h}" for h in HORIZONS),
    *(f"twin_headroom_{h}" for h in HORIZONS),
    *(f"d{k}" for k in LAGS),
    "slope10", "std10", "log_capacity", "critical", "headroom", "history_frac",
)
HISTORY_FULL = 240


def booster_file(h: int, q: float) -> str:
    return f"h{h}_q{int(round(q * 100)):02d}.txt.gz"


def save_booster(booster: Any, path: Path, num_iteration: int | None = None) -> None:
    """Deterministic bytes (no gzip timestamp), so the manifest hash is reproducible."""
    text = booster.model_to_string(num_iteration=num_iteration).encode("utf-8")
    with open(path, "wb") as f, gzip.GzipFile(fileobj=f, mode="wb", mtime=0, filename="") as gz:
        gz.write(text)


def features(history: list[float], capacity: float, critical: float, prior: dict,
             twin: dict[int, float]) -> list[float]:
    """One entity's feature row. `history`: its utilisation series, latest last
    (at least 2 readings); `prior`: its `model_prior` entry {"now", "points"};
    `twin`: the twin-model forecast at each of HORIZONS."""
    last = float(history[-1])
    prior_now = float(prior.get("now", last))
    pts = prior.get("points") or {}
    plan = {h: float(pts.get(h, pts.get(str(h), prior_now))) for h in HORIZONS}
    lag = [last - float(history[-1 - k]) if len(history) > k else math.nan for k in LAGS]
    w = np.asarray(history[-10:], dtype=float)
    if len(w) >= 3:
        x = np.arange(len(w)) - (len(w) - 1) / 2.0
        slope, std = float((x * (w - w.mean())).sum() / (x * x).sum()), float(w.std())
    else:
        slope, std = math.nan, math.nan
    return [
        last, prior_now, last - prior_now,
        *(float(twin[h]) for h in HORIZONS),
        *(plan[h] - prior_now for h in HORIZONS),
        *(critical - float(twin[h]) for h in HORIZONS),
        *lag, slope, std,
        math.log1p(max(float(capacity or 0.0), 0.0)), float(critical), critical - last,
        min(len(history), HISTORY_FULL) / HISTORY_FULL,
    ]


class CorrectionUnavailable(Exception):
    """The bundle cannot be served (missing, unverified, or did not pass its gate)."""


class ForecastCorrector:
    def __init__(self, bundle_dir: str | Path, require_gate: bool = True) -> None:
        import lightgbm as lgb  # optional dependency: absent -> the caller keeps the twin model

        from ML.manifest import ManifestError, load_manifest

        bundle = Path(bundle_dir)
        names = [booster_file(h, q) for h in HORIZONS for q in QUANTILES]
        try:
            manifest = load_manifest(bundle, required=("features.json", *names))
        except ManifestError as exc:
            raise CorrectionUnavailable(str(exc)) from exc
        layout = json.loads((bundle / "features.json").read_text(encoding="utf-8"))
        if (tuple(layout.get("feature_names") or ()) != FEATURE_NAMES
                or tuple(layout.get("horizons_sec") or ()) != HORIZONS
                or tuple(layout.get("quantiles") or ()) != QUANTILES):
            raise CorrectionUnavailable("feature layout differs from ML/forecast_correction.py")
        if require_gate:
            gate = (json.loads((bundle / "eval.json").read_text(encoding="utf-8")).get("gate") or {}
                    if "eval.json" in manifest["files"] else {})
            if gate.get("passes") is not True:
                raise CorrectionUnavailable("eval.json gate did not pass; the twin model stays in service")
        self.manifest = manifest
        self._boosters = {(h, q): lgb.Booster(model_str=gzip.decompress(
                              (bundle / booster_file(h, q)).read_bytes()).decode("utf-8"))
                          for h in HORIZONS for q in QUANTILES}
        sha = manifest["files"][booster_file(1800, 0.5)]["sha256"][:8]
        self.version = f"{manifest.get('model_version')}@{sha}"

    def correct(self, rows: list[list[float]], twin: np.ndarray) -> np.ndarray:
        """`twin` (n, len(HORIZONS)) -> corrected quantiles (n, len(HORIZONS), 3),
        monotone in the quantile."""
        X = np.asarray(rows, dtype=float)
        out = np.empty((len(X), len(HORIZONS), len(QUANTILES)))
        for i, h in enumerate(HORIZONS):
            for j, q in enumerate(QUANTILES):
                out[:, i, j] = twin[:, i] + self._boosters[(h, q)].predict(X, num_threads=1)
        return np.sort(out, axis=-1)
