"""Evaluation of failure predictors on the v3 dataset — one definition for all.

A *sample* is (snapshot, entity) for the cascade-relevant types (gate, road,
transport_node, emergency_facility) that are NOT already over their critical
line in the published state at the snapshot — the same rule the backend's
online evaluator uses for alerts (an entity already over its line is not a
prediction). The label is ground truth: did it reach the line within h seconds.

Per horizon h (900 / 1800 / 3600 s):
  precision, recall, F1  at the predictor's operating point
  average precision      from its scores
  ECE                    expected calibration error (probabilistic predictors only)
At 3600 s:
  event recall, lead time  per actual crossing: caught if some snapshot in the hour
                           before it alerted on the entity; lead = crossing - first alert
  TTC MAE                  |predicted - true| time to critical, on true positives

Every metric carries a 95% interval from a cluster bootstrap over maps (over
runs when a split has one map), because snapshots of one map are correlated.
Everything is in simulation, never field validity (03 §8.4).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

HORIZONS = (900, 1800, 3600)


@dataclass
class Run:
    """One simulated run, already reduced to the evaluation samples."""
    cluster: str                       # map id (or run id for a single-map split)
    mask: np.ndarray                   # (S, N) bool — evaluated samples
    labels: np.ndarray                 # (S, N, 3) bool
    ttc_true: np.ndarray               # (S, N) float, NaN = no crossing within 3600 s
    snapshot_step: np.ndarray          # (S,)
    crossings: np.ndarray              # (M, 2) [step, entity] of scored entities
    cycle_sec: int
    predictions: dict[str, dict[str, np.ndarray]] = field(default_factory=dict)
    # predictions[name] = {"scores": (S,N,3), "alerts": (S,N,3) bool, "ttc": (S,N) NaN=none}


# --- point metrics --------------------------------------------------------------------------
def average_precision(scores: np.ndarray, labels: np.ndarray) -> float:
    if labels.sum() == 0:
        return float("nan")
    order = np.argsort(-scores, kind="mergesort")
    y = labels[order].astype(np.float64)
    tp = np.cumsum(y)
    precision = tp / np.arange(1, len(y) + 1)
    return float((precision * y).sum() / y.sum())


def expected_calibration_error(p: np.ndarray, y: np.ndarray, bins: int = 15) -> float:
    if len(p) == 0:
        return float("nan")
    edges = np.linspace(0.0, 1.0, bins + 1)
    which = np.clip(np.digitize(p, edges[1:-1]), 0, bins - 1)
    ece = 0.0
    for b in range(bins):
        sel = which == b
        if sel.any():
            ece += sel.mean() * abs(p[sel].mean() - y[sel].mean())
    return float(ece)


def _pool(runs: list[Run], name: str) -> dict[str, np.ndarray]:
    """Concatenate one predictor's samples across runs."""
    out: dict[str, list] = {"labels": [], "scores": [], "alerts": [], "ttc_true": [], "ttc": []}
    for r in runs:
        p = r.predictions[name]
        m = r.mask
        out["labels"].append(r.labels[m])
        out["scores"].append(np.nan_to_num(p["scores"][m], nan=0.0))
        out["alerts"].append(p["alerts"][m])
        out["ttc_true"].append(r.ttc_true[m])
        out["ttc"].append(p["ttc"][m])
    return {k: np.concatenate(v) if v else np.zeros(0) for k, v in out.items()}


def _events(runs: list[Run], name: str, horizon_sec: int = 3600) -> tuple[int, int, list[float]]:
    caught = total = 0
    leads: list[float] = []
    for r in runs:
        alerts = r.predictions[name]["alerts"][..., HORIZONS.index(horizon_sec)] & r.mask
        window = horizon_sec // r.cycle_sec
        steps = r.snapshot_step
        for t, e in r.crossings:
            in_window = (steps < t) & (steps >= t - window)
            if not in_window.any():
                continue   # no snapshot in the hour before this crossing: cannot be scored
            total += 1
            hit = np.nonzero(in_window & alerts[:, e])[0]
            if len(hit):
                caught += 1
                leads.append(float((t - steps[hit[0]]) * r.cycle_sec))
    return caught, total, leads


def point_metrics(runs: list[Run], name: str, probabilistic: bool) -> dict[str, Any]:
    pool = _pool(runs, name)
    out: dict[str, Any] = {"n_samples": int(len(pool["labels"]))}
    for j, h in enumerate(HORIZONS):
        y = pool["labels"][:, j] if len(pool["labels"]) else np.zeros(0, bool)
        a = pool["alerts"][:, j] if len(pool["alerts"]) else np.zeros(0, bool)
        s = pool["scores"][:, j] if len(pool["scores"]) else np.zeros(0)
        tp, fp, fn = int((a & y).sum()), int((a & ~y).sum()), int((~a & y).sum())
        prec = tp / (tp + fp) if tp + fp else float("nan")
        rec = tp / (tp + fn) if tp + fn else float("nan")
        out[str(h)] = {
            "precision": prec, "recall": rec,
            "f1": 2 * prec * rec / (prec + rec) if prec == prec and rec == rec and prec + rec else float("nan"),
            "average_precision": average_precision(s, y),
            "ece": expected_calibration_error(s, y.astype(float)) if probabilistic else None,
            "positives": int(y.sum()), "alerts": int(a.sum()),
        }
    y3 = pool["labels"][:, 2] if len(pool["labels"]) else np.zeros(0, bool)
    a3 = pool["alerts"][:, 2] if len(pool["alerts"]) else np.zeros(0, bool)
    both = y3 & a3 & ~np.isnan(pool["ttc"]) if len(pool["ttc"]) else np.zeros(0, bool)
    out["ttc_mae_sec"] = float(np.abs(pool["ttc"][both] - pool["ttc_true"][both]).mean()) if both.any() else float("nan")
    out["ttc_n"] = int(both.sum())
    caught, total, leads = _events(runs, name)
    out["event_recall"] = caught / total if total else float("nan")
    out["events"] = total
    out["lead_time_sec"] = float(np.mean(leads)) if leads else float("nan")
    return out


def _flatten(m: dict[str, Any], prefix: str = "") -> dict[str, float]:
    flat: dict[str, float] = {}
    for k, v in m.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            flat.update(_flatten(v, key + "."))
        elif isinstance(v, (int, float)) and v is not None:
            flat[key] = float(v)
    return flat


def evaluate(runs: list[Run], name: str, probabilistic: bool, bootstrap: int = 200, seed: int = 0) -> dict[str, Any]:
    """Point metrics plus a 95% cluster-bootstrap interval for every numeric one."""
    point = point_metrics(runs, name, probabilistic)
    clusters = sorted({r.cluster for r in runs})
    by_cluster = {c: [r for r in runs if r.cluster == c] for c in clusters}
    rng = np.random.default_rng(seed)
    samples: dict[str, list[float]] = {}
    if len(clusters) > 1 and bootstrap > 0:
        for _ in range(bootstrap):
            pick = rng.choice(len(clusters), size=len(clusters), replace=True)
            resampled = [r for i in pick for r in by_cluster[clusters[i]]]
            for k, v in _flatten(point_metrics(resampled, name, probabilistic)).items():
                if v == v:   # skip NaN
                    samples.setdefault(k, []).append(v)
    ci = {k: [round(float(np.percentile(v, 2.5)), 4), round(float(np.percentile(v, 97.5)), 4)]
          for k, v in samples.items() if len(v) >= 10}
    return {"point": _round(point), "ci95": ci, "clusters": len(clusters),
            "ci_note": None if ci else "not enough clusters for an interval"}


def _round(m: Any) -> Any:
    if isinstance(m, dict):
        return {k: _round(v) for k, v in m.items()}
    if isinstance(m, float):
        return None if m != m else round(m, 4)
    return m


# --- baselines from the recorded arrays ---------------------------------------------------------
def baseline_predictions(npz: dict[str, np.ndarray], v2_threshold: float) -> dict[str, dict[str, np.ndarray]]:
    """The three recorded baselines in the common prediction format."""
    crit = npz["critical"][None, :]
    out: dict[str, dict[str, np.ndarray]] = {}

    det_eta, det_score = npz["det_eta"], npz["det_score"]
    alerts = np.stack([~np.isnan(det_eta) & (np.nan_to_num(det_eta, nan=1e9) <= h) for h in HORIZONS], axis=-1)
    # Score: the cascade's own failure_probability; steps it projects critical in time rank above others.
    scores = np.where(alerts, 1.0 + det_score[..., None], det_score[..., None] * 0.5)
    out["deterministic_cascade"] = {"scores": scores, "alerts": alerts, "ttc": det_eta}

    fc = npz["forecasts"]
    fc_ttc = npz["fc_ttc"]
    headroom = np.nan_to_num(fc, nan=-9.0) - crit[..., None]
    fr_scores = np.maximum.accumulate(headroom, axis=-1)
    fr_alerts = np.stack([~np.isnan(fc_ttc) & (np.nan_to_num(fc_ttc, nan=1e9) <= h) for h in HORIZONS], axis=-1)
    out["forecast_rule"] = {"scores": fr_scores, "alerts": fr_alerts, "ttc": fc_ttc}

    v2p = npz["v2_p"]
    if not np.isnan(v2p).all():
        out["hx_cascade_v2"] = {"scores": np.nan_to_num(v2p, nan=0.0), "alerts": np.nan_to_num(v2p, nan=0.0) >= v2_threshold,
                                "ttc": npz["v2_ttc"]}
    return out


def sample_mask(npz: dict[str, np.ndarray], scored_types: tuple[str, ...]) -> np.ndarray:
    """Scored entity types, not over their line in the published state."""
    scored = np.isin(npz["types"], np.array(scored_types))
    return scored[None, :] & (npz["util"] < npz["critical"][None, :])


def scored_crossings(npz: dict[str, np.ndarray], scored_types: tuple[str, ...]) -> np.ndarray:
    scored = np.isin(npz["types"], np.array(scored_types))
    c = npz["crossings"]
    return c[scored[c[:, 1]]] if len(c) else c.reshape(0, 2)


def swap_criterion(results: dict[str, Any], candidate: str, baseline: str = "deterministic_cascade",
                   min_precision: float = 0.75, min_recall: float = 0.70) -> dict[str, Any]:
    """03 §4.3 on the held-out-map test split: the candidate must beat the
    deterministic propagator on precision AND time-to-critical error, and meet
    the §4.4 precision / recall floors. Evidence only — the swap is a human decision."""
    c, b = results[candidate]["point"], results[baseline]["point"]
    cp, bp = c["3600"]["precision"], b["3600"]["precision"]
    checks = {
        "precision_3600_above_baseline": cp is not None and bp is not None and cp > bp,
        "ttc_mae_below_baseline": (c["ttc_mae_sec"] is not None and b["ttc_mae_sec"] is not None
                                   and c["ttc_mae_sec"] < b["ttc_mae_sec"]),
        "precision_3600_at_least_%.2f" % min_precision: cp is not None and cp >= min_precision,
        "recall_3600_at_least_%.2f" % min_recall: (c["3600"]["recall"] or 0) >= min_recall,
    }
    return {"candidate": candidate, "baseline": baseline, "checks": checks, "passes": all(checks.values())}
