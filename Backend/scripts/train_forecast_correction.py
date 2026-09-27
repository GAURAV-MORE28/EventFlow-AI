"""Train, evaluate and gate the forecast-correction model (ML/forecast_correction.py).

    cd Backend
    python -m scripts.forecast_dataset --out ../data/forecast_v2      # the data
    python -m scripts.train_forecast_correction --data ../data/forecast_v2 --out ../data/forecast_correction_v2

LightGBM quantile regression (5 / 50 / 95 %) of the twin model's residual,
one booster per horizon and quantile, early-stopped on the validation maps.
Evaluated on the held-out test maps and on the two live-map runs (the demo
scenario; never trained on), against the twin model and persistence, through
the same `ForecastCorrector` the forecaster serves with:

  mae_<h>            |forecast - true utilisation at t+h|
  coverage_90        share of true values inside the model's 5-95 % band
  ttc_error_sec      time_to_critical_sec as served (03 §2.4 on the dense forecast
                     path), over rows below the critical line with a true crossing
                     within 3600 s: |min(predicted time_to_critical, 3600) -
                     true time|, a missing prediction counting as 3600
                     (so a model cannot improve it by never predicting)
  false_ttc_rate     rows with no crossing within 3600 s that got a prediction

The ship gate (config `forecaster.correction_gate`; 03 §2.5): the model must beat
the twin model (not persistence) on 1800 s error and time-to-critical error on
the test maps, not be worse than it on the demo scenario, and its demo
time-to-critical error must be at most `ttc_max_error_sec` (180). The bundle is
written either way, with the verdict in eval.json; `ForecastCorrector` refuses
to serve a bundle whose gate did not pass.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

import numpy as np

from scripts.cascade_dataset import REPO, _git_commit, _git_dirty  # noqa: F401  (puts the repo on sys.path)
from app.config import get_config
from app.ml_reference.forecaster import Forecaster
from ML.forecast_correction import (
    FEATURE_NAMES, HORIZONS, QUANTILES, ForecastCorrector, booster_file, save_booster,
)
from ML.manifest import write_manifest

MODEL_VERSION = "forecast_correction_v2"
KEYS = ("X", "twin", "twin_ttc", "twin_path", "actual", "observed", "ttc_true", "over_now", "critical")


def load(data: Path, split: str) -> dict[str, np.ndarray]:
    parts: dict[str, list] = {k: [] for k in (*KEYS, "run")}
    for r, f in enumerate(sorted((data / split).glob("*.npz"))):
        d = np.load(f)
        for k in KEYS:
            parts[k].append(d[k][d["entity"]] if k == "critical" else d[k])
        parts["run"].append(np.full(len(d["X"]), r, dtype=np.int32))
    if not parts["run"]:
        raise SystemExit(f"no runs in {data / split}")
    return {k: np.concatenate(v) for k, v in parts.items()}


def train(tr: dict, va: dict, bundle: Path, seed: int, threads: int) -> dict[str, Any]:
    import lightgbm as lgb

    info: dict[str, Any] = {}
    for i, h in enumerate(HORIZONS):
        y_tr, y_va = tr["actual"][:, i] - tr["twin"][:, i], va["actual"][:, i] - va["twin"][:, i]
        for q in QUANTILES:
            params = {
                "objective": "quantile", "alpha": q, "learning_rate": 0.05, "num_leaves": 31,
                "min_data_in_leaf": 200, "feature_fraction": 0.9, "bagging_fraction": 0.8, "bagging_freq": 1,
                "lambda_l2": 1.0, "seed": seed, "deterministic": True, "force_row_wise": True,
                "num_threads": threads, "verbose": -1,
            }
            ds = lgb.Dataset(tr["X"], y_tr, feature_name=list(FEATURE_NAMES), free_raw_data=False)
            dv = lgb.Dataset(va["X"], y_va, reference=ds)
            booster = lgb.train(params, ds, num_boost_round=3000, valid_sets=[dv],
                                callbacks=[lgb.early_stopping(100, verbose=False)])
            save_booster(booster, bundle / booster_file(h, q), booster.best_iteration)
            info[booster_file(h, q)] = {"best_iteration": booster.best_iteration,
                                        "val_quantile_loss": round(float(booster.best_score["valid_0"]["quantile"]), 6)}
            print(f"  h={h} q={q}: {booster.best_iteration} trees", flush=True)
    return info


def corrected(corrector: ForecastCorrector, d: dict) -> np.ndarray:
    """(n, horizons, quantiles), clamped and rounded as the forecaster serves them."""
    return np.round(np.clip(corrector.correct(d["X"], d["twin"].astype(float)), 0.0, 1.8), 4)


def ttc(fc: Forecaster, d: dict, values: np.ndarray | None) -> np.ndarray:
    """time_to_critical_sec exactly as the forecaster serves it (03 §2.4 on the
    dense path): the twin path, shifted by the correction when `values` (the
    corrected points) are given. Persistence: flat at the last reading. NaN = none."""
    last = d["X"][:, FEATURE_NAMES.index("last")].astype(float)
    ts = [60 * (k + 1) for k in range(d["twin_path"].shape[1] - 1)]
    out = np.full(len(last), np.nan)
    fc.critical_by_entity = {}
    for r in range(len(last)):
        fc.critical = float(d["critical"][r])
        path = list(zip([0, *ts], d["twin_path"][r].astype(float)))
        if values is not None:
            path = fc.corrected_path(path, list(HORIZONS), list(d["twin"][r].astype(float)), list(values[r]))
        t = fc._path_ttc(float(last[r]), path, "")
        out[r] = np.nan if t is None else t
    return out


def score(fc: Forecaster, d: dict, model: np.ndarray) -> dict[str, Any]:
    last = d["X"][:, FEATURE_NAMES.index("last")].astype(float)
    preds = {"persistence": np.repeat(last[:, None], len(HORIZONS), axis=1),
             "twin_model": d["twin"].astype(float), "model": model[:, :, 1]}
    crosses = ~d["over_now"] & ~np.isnan(d["ttc_true"])
    quiet = ~d["over_now"] & np.isnan(d["ttc_true"])
    out: dict[str, Any] = {"rows": int(len(last)), "ttc_rows": int(crosses.sum())}
    for name, p in preds.items():
        m: dict[str, Any] = {f"mae_{h}": round(float(np.abs(p[:, j] - d["actual"][:, j]).mean()), 5)
                             for j, h in enumerate(HORIZONS)}
        m["mae_1800_vs_observed"] = round(float(np.abs(p[:, 1] - d["observed"][:, 1]).mean()), 5)
        if name == "persistence":   # flat at the last reading
            t = ttc(fc, {**d, "twin_path": np.repeat(last[:, None], d["twin_path"].shape[1], 1)}, None)
        else:
            t = ttc(fc, d, p if name == "model" else None)
            if name == "twin_model":
                # The scorer must reproduce what was served. The logged path is
                # float32, so a path peaking within float32 precision of the line
                # may interpolate its crossing a few seconds differently; only
                # those rows are exempt, and they are counted.
                differs = (np.isnan(t) != np.isnan(d["twin_ttc"])) |                     (np.abs(np.nan_to_num(t, nan=-1) - np.nan_to_num(d["twin_ttc"], nan=-1)) > 1.0)
                borderline = np.abs(d["twin_path"].max(axis=1) - d["critical"]) < 1e-4
                assert not (differs & ~borderline).any(), "twin TTC differs from the served value"
                out["float32_borderline_rows"] = int((differs & borderline).sum())
        err = np.abs(np.nan_to_num(np.minimum(t, 3600), nan=3600) - d["ttc_true"])[crosses]
        m["ttc_error_sec"] = round(float(err.mean()), 1) if err.size else None
        m["ttc_error_median_sec"] = round(float(np.median(err)), 1) if err.size else None
        m["ttc_detected"] = round(float((~np.isnan(t[crosses])).mean()), 3) if err.size else None
        m["false_ttc_rate"] = round(float((~np.isnan(t[quiet])).mean()), 4) if quiet.any() else None
        out[name] = m
    lo, hi = model[:, :, 0], model[:, :, 2]
    out["model"]["coverage_90"] = {str(h): round(float(((d["actual"][:, j] >= lo[:, j]) & (d["actual"][:, j] <= hi[:, j])).mean()), 3)
                                   for j, h in enumerate(HORIZONS)}
    out["model"]["band_width_90"] = {str(h): round(float((hi[:, j] - lo[:, j]).mean()), 4) for j, h in enumerate(HORIZONS)}
    return out


def gate(test: dict, demo: dict, limits: dict) -> dict[str, Any]:
    cap = float(limits.get("ttc_max_error_sec", 180))
    m, t = test["model"], test["twin_model"]
    dm, dt = demo["model"], demo["twin_model"]
    checks = {
        "test.mae_1800_below_twin_model": m["mae_1800"] < t["mae_1800"],
        "test.ttc_error_below_twin_model": m["ttc_error_sec"] is not None and m["ttc_error_sec"] < t["ttc_error_sec"],
        "demo.mae_1800_not_above_twin_model": dm["mae_1800"] <= dt["mae_1800"],
        "demo.ttc_error_not_above_twin_model": dm["ttc_error_sec"] is not None and dm["ttc_error_sec"] <= dt["ttc_error_sec"],
        f"demo.ttc_error_at_most_{cap:g}s": dm["ttc_error_sec"] is not None and dm["ttc_error_sec"] <= cap,
    }
    return {"checks": checks, "passes": all(checks.values()), "limits": {"ttc_max_error_sec": cap}}


def latency_ms(corrector: ForecastCorrector, d: dict, n: int = 67, reps: int = 200) -> dict[str, float]:
    """One forecast cycle's correction (n entities, the live map's size)."""
    X, twin = d["X"][:n], d["twin"][:n].astype(float)
    times = []
    for _ in range(reps):
        t0 = time.perf_counter()
        corrector.correct(X, twin)
        times.append((time.perf_counter() - t0) * 1000)
    return {"p50": round(float(np.percentile(times, 50)), 2), "p95": round(float(np.percentile(times, 95)), 2),
            "forecast_budget_ms": float(get_config().raw["budgets_ms"]["forecast"])}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default=str(REPO / "data" / "forecast_v2"))
    ap.add_argument("--out", default=None, help="bundle directory (default ML/artifacts/<version>)")
    ap.add_argument("--version", default=MODEL_VERSION)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--threads", type=int, default=4)
    args = ap.parse_args()

    data, bundle = Path(args.data), Path(args.out or REPO / "ML" / "artifacts" / args.version)
    bundle.mkdir(parents=True, exist_ok=True)
    for f in bundle.iterdir():
        f.unlink()
    splits = {s: load(data, s) for s in ("train", "val", "test", "demo")}
    print({s: len(d["X"]) for s, d in splits.items()}, flush=True)

    started = time.perf_counter()
    training = train(splits["train"], splits["val"], bundle, args.seed, args.threads)
    (bundle / "features.json").write_text(json.dumps({
        "feature_names": list(FEATURE_NAMES), "horizons_sec": list(HORIZONS), "quantiles": list(QUANTILES),
        "target": "true utilisation at t+h minus the twin-model forecast at h"}, indent=1) + "\n", encoding="utf-8")
    write_manifest(bundle, model_name="forecast_correction", model_version=args.version)   # for the evaluation load
    corrector = ForecastCorrector(bundle, require_gate=False)

    fc = Forecaster({})
    results = {("demo_scenario" if s == "demo" else s): score(fc, d, corrected(corrector, d))
               for s, d in splits.items() if s != "train"}
    demo_runs = sorted(p.stem for p in (data / "demo").glob("*.npz"))
    for r, name in enumerate(demo_runs):
        d = {k: v[splits["demo"]["run"] == r] for k, v in splits["demo"].items()}
        results[name] = score(fc, d, corrected(corrector, d))
    limits = get_config().raw.get("forecaster", {}).get("correction_gate", {})
    verdict = gate(results["test"], results["demo_scenario"], limits)
    dataset = json.loads((data / "dataset.json").read_text(encoding="utf-8")) if (data / "dataset.json").exists() else {}
    report = {
        "model_version": args.version, "gate": verdict, "results": results,
        "latency_ms": latency_ms(corrector, splits["test"]),
        "splits": {s: {"rows": int(len(d["X"])), "runs": int(d["run"].max() + 1)} for s, d in splits.items()},
        "definitions": "see Backend/scripts/train_forecast_correction.py; truth = simulator ground truth; "
                       "demo_scenario = the two live-map runs pooled (demo: plain, demo_disrupted: "
                       "seeded disruptions), never trained on",
        "in_simulation_only": True,
    }
    (bundle / "eval.json").write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
    write_manifest(
        bundle, model_name="forecast_correction", model_version=args.version,
        trained_on=f"{report['splits']['train']['runs']} simulated runs on random maps (train); "
                   f"{report['splits']['val']['runs']} validation runs (early stopping); test maps and the live map held out",
        training={"seconds": round(time.perf_counter() - started, 1), "seed": args.seed, "boosters": training},
        dataset={"path": str(data), "source_commit": dataset.get("source_commit"),
                 "source_dirty": dataset.get("source_dirty")},
        source_commit=_git_commit(), created_by="Backend/scripts/train_forecast_correction.py",
    )
    results_dir = REPO / "ML" / "evaluation" / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    (results_dir / f"{args.version}.json").write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")

    for s in ("test", "demo_scenario", *demo_runs):
        r = results[s]
        print(f"{s:16s} " + "  ".join(
            f"{n}: mae1800={r[n]['mae_1800']:.4f} ttc_err={r[n]['ttc_error_sec']}"
            for n in ("persistence", "twin_model", "model")) + f"  (ttc rows {r['ttc_rows']})")
    print("gate:", json.dumps(verdict["checks"], indent=1), "passes =", verdict["passes"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
