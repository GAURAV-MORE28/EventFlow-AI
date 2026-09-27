"""Evidence for swapping a cascade model from `shadow` to `annotate` (03 §4.3, §4.5).

    cd Backend
    python -m scripts.cascade_swap_eval reproduce --data ../data/cascade_v3_small --maps 3
    python -m scripts.cascade_swap_eval shadow

Both subcommands write JSON to ML/evaluation/results/, which the swap gate
(`tests/test_cascade_swap_gate.py`) reads beside the bundle's eval.json.

reproduce  Are the bundle's numbers real?
  1. Re-scores the whole test split through the SERVING adapter (`ML/cascade.py`
     `node_risk`, the call the engine makes every cycle) instead of training's
     batched path, and compares every point metric with eval.json.
  2. Re-simulates a sample of test maps from the seeds recorded in the dataset
     and checks the regenerated runs equal the stored ones, so (1) rests on data
     that can be rebuilt, not on files that merely exist.

shadow     How does the model behave inside the live cycle?
  One full simulated event (720 cycles, 14:00-20:00) on the live map, driven by
  the real engine with the model in `shadow` mode: every cycle's published
  deterministic cascade, the model's `store.cascade_ml` (exactly what the engine
  persists to `ml_node_prediction`) and the comparison model are saved to an
  .npz, then scored from that file with the offline definition
  (`ML/evaluation/cascade_eval.py`) and, per cycle, with the online one
  (`services/prediction_eval.py`, what /metrics reports). Two runs: the plain
  demo event and the same event with a seeded disruption plan.

In simulation only; never field validity (03 §8.4).
"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "sqlite://")

import argparse  # noqa: E402
import asyncio  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402
import random  # noqa: E402
import time  # noqa: E402
from concurrent.futures import ProcessPoolExecutor  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any  # noqa: E402

import numpy as np  # noqa: E402

from scripts.cascade_dataset import (  # noqa: E402  (also puts Backend/ and the repo on sys.path)
    HORIZONS, REPO, GeneratedCity, HeadlessEngine, _apply, _plan, _simulate, _unbounded_budgets,
)
from app.config import get_config  # noqa: E402
from app.ml_registry import MLRegistry  # noqa: E402
from app.services.engine import Engine  # noqa: E402
from app.services.prediction_eval import SCORED_TYPES, OnlineEvaluator  # noqa: E402
from app.topology import build_topology  # noqa: E402
from ML.evaluation import cascade_eval as ev  # noqa: E402
from ML.manifest import load_manifest, topology_hash  # noqa: E402

RESULTS = REPO / "ML" / "evaluation" / "results"
V2_BUNDLE = "ML/artifacts/hx_cascade_v2"
V3_BUNDLE = "ML/artifacts/hx_cascade_v3"
# Serving rounds probabilities to 4 decimals and TTC to whole seconds, so an alert
# exactly at a threshold can flip; these bound what that may move.
TOL_RATE = 0.01
TOL_SEC = 5.0
log = logging.getLogger("eventflow.swap_eval")


def predictor(bundle: str):
    """The serving `CascadePredictor`, built by the registry exactly as the backend
    builds it, for `bundle`."""
    cascade_cfg = get_config().raw["cascade"]
    saved = cascade_cfg.get("gnn_artifact")
    cascade_cfg["gnn_artifact"] = bundle
    try:
        p = MLRegistry().cascade
    finally:
        cascade_cfg["gnn_artifact"] = saved
    if not (getattr(p, "ready", lambda: False)() and hasattr(p, "node_risk")):
        raise SystemExit(f"{bundle} did not load: {getattr(p, 'model_info', dict)().get('error')}")
    return p


def _eval_json(bundle: str) -> dict[str, Any]:
    return json.loads((REPO / bundle / "eval.json").read_text(encoding="utf-8"))


def _node_state(npz: dict[str, np.ndarray], s: int) -> dict[str, dict]:
    """A recorded snapshot back into the `node_state_for_ml()` fields the model reads."""
    nan = lambda v: None if v != v else float(v)  # noqa: E731
    return {str(e): {"entity_type": str(t), "utilisation": float(npz["util"][s, i]),
                     **{f"forecast_{h}": nan(npz["forecasts"][s, i, j]) for j, h in enumerate(HORIZONS)},
                     "nominal_capacity": float(npz["capacity"][i]), "is_observed": bool(npz["is_observed"][s, i]),
                     "flow_rate_per_min": float(npz["flow"][s, i])}
            for i, (e, t) in enumerate(zip(npz["entity_ids"], npz["types"]))}


def _model_arrays(risks: list[dict], ids: list[str], thresholds: dict[int, float]) -> dict[str, np.ndarray]:
    """A sequence of `node_risk()` outputs -> the common prediction format."""
    pos = {e: i for i, e in enumerate(ids)}
    p = np.full((len(risks), len(ids), 3), np.nan, dtype=np.float64)
    ttc = np.full((len(risks), len(ids)), np.nan, dtype=np.float64)
    for s, risk in enumerate(risks):
        for e, row in ((risk or {}).get("nodes") or {}).items():
            if e in pos and row.get("p_fail_3600") is not None:
                p[s, pos[e]] = [row[f"p_fail_{h}"] for h in HORIZONS]
                ttc[s, pos[e]] = np.nan if row.get("ttc_sec") is None else row["ttc_sec"]
    scores = np.nan_to_num(p, nan=0.0)
    return {"scores": scores, "alerts": scores >= np.array([thresholds[h] for h in HORIZONS])[None, None, :],
            "ttc": ttc}


def _load(path: Path) -> dict[str, np.ndarray]:
    with np.load(path, allow_pickle=False) as d:
        return {k: d[k] for k in d.files}


def _compare(ours: dict[str, Any], theirs: dict[str, Any]) -> tuple[bool, list[dict]]:
    """Every point metric in eval.json against ours; seconds and rates have their own tolerance."""
    rows, ok = [], True
    keys = [(str(h), m) for h in HORIZONS for m in ("precision", "recall", "average_precision", "ece")]
    keys += [(None, m) for m in ("ttc_mae_sec", "event_recall", "lead_time_sec")]
    for h, m in keys:
        a = (ours[h] if h else ours).get(m)
        b = (theirs[h] if h else theirs).get(m)
        if a is None and b is None:
            continue
        tol = TOL_SEC if m.endswith("_sec") else TOL_RATE
        good = a is not None and b is not None and abs(a - b) <= tol
        ok &= good
        rows.append({"metric": f"{h}.{m}" if h else m, "reproduced": a, "eval_json": b, "tolerance": tol,
                     "match": good})
    return ok, rows


# --- reproduce ----------------------------------------------------------------------------------
# Arrays fixed by the simulator's ground truth and the published deterministic
# cascade; the rest (sensor readings, EnKF estimates, forecasts) carries the
# twin's seeded noise.
TRUTH_KEYS = ("entity_ids", "types", "capacity", "critical", "cycle_sec", "snapshot_step", "labels", "ttc_true",
              "crossings", "truth_over_at_snapshot")


def _resimulate(task: dict) -> dict:
    """One stored test run, simulated again from its recorded map and seed (with
    the comparison model the dataset recorded, v2) and saved beside the original."""
    logging.basicConfig(level=logging.ERROR)
    logging.getLogger().setLevel(logging.ERROR)
    _unbounded_budgets()
    get_config().raw["cascade"]["gnn_artifact"] = V2_BUNDLE
    map_rec = json.loads(Path(task["map_path"]).read_text(encoding="utf-8"))
    fresh = asyncio.run(_simulate(map_rec, task["scenario_seed"], task["steps"], task["snap_every"], task["warmup"]))
    meta = fresh.pop("_meta")
    np.savez_compressed(task["out_path"], **fresh, meta=np.array(json.dumps(
        {**meta, "map_id": map_rec["map_id"], "split": "test", "scenario_seed": task["scenario_seed"]})))
    stored = _load(Path(task["npz_path"]))
    truth_equal, input_diff = True, {}
    for k, v in fresh.items():
        a, b = np.asarray(v), stored[k]
        if k in TRUTH_KEYS:
            truth_equal &= a.shape == b.shape and bool(np.array_equal(a, b, equal_nan=a.dtype.kind == "f"))
        elif a.shape == b.shape and a.dtype.kind == "f" and not np.isnan(a).all():
            input_diff[k] = round(float(np.nanmax(np.abs(a - b))), 5)
    return {"run": Path(task["npz_path"]).name, "truth_equal": truth_equal, "input_max_abs_diff": input_diff}


def _runs(files: list[Path], data: Path, model, name: str, thresholds: dict[int, float],
          v2_threshold: float) -> list[ev.Run]:
    """Stored (or regenerated) runs, the model scored through the serving adapter."""
    runs, edges_by_map = [], {}
    for f in files:
        npz = _load(f)
        meta = json.loads(str(npz.pop("meta")))
        if meta["map_id"] not in edges_by_map:
            rec = json.loads((data / "maps" / f"{meta['map_id']}.json").read_text(encoding="utf-8"))
            edges_by_map[meta["map_id"]] = rec["topology"]["edges"]
        ids = [str(e) for e in npz["entity_ids"]]
        risks = [model.node_risk(_node_state(npz, s), edges_by_map[meta["map_id"]])
                 for s in range(len(npz["snapshot_step"]))]
        if any(r.get("source") != "gnn" for r in risks):
            raise SystemExit(f"{f.name}: the model fell back on some snapshots; not a reproduction")
        runs.append(ev.Run(cluster=meta["map_id"], mask=ev.sample_mask(npz, tuple(SCORED_TYPES)),
                           labels=npz["labels"], ttc_true=npz["ttc_true"], snapshot_step=npz["snapshot_step"],
                           crossings=ev.scored_crossings(npz, tuple(SCORED_TYPES)), cycle_sec=int(npz["cycle_sec"]),
                           predictions={name: _model_arrays(risks, ids, thresholds),
                                        **ev.baseline_predictions(npz, v2_threshold)}))
    return runs


def _point(runs: list[ev.Run], name: str) -> dict[str, Any]:
    return ev._round(ev.point_metrics(runs, name, probabilistic=name.startswith("hx_cascade")))


def reproduce(args: argparse.Namespace) -> dict[str, Any]:
    data = Path(args.data).resolve()
    dataset = json.loads((data / "dataset.json").read_text(encoding="utf-8"))
    bundle_eval = _eval_json(args.bundle)
    name = load_manifest(REPO / args.bundle)["model_version"]
    thresholds = {int(h): float(t) for h, t in bundle_eval["operating_points"][name]["thresholds"].items()}
    v2_threshold = float(bundle_eval["operating_points"]["hx_cascade_v2"]["threshold"])
    model = predictor(args.bundle)

    # 1. the whole test split through the serving adapter, against eval.json
    started = time.perf_counter()
    files = sorted((data / "scenarios" / "test").glob("*.npz"))
    runs = _runs(files, data, model, name, thresholds, v2_threshold)
    serving: dict[str, Any] = {}
    serving_ok = True
    for pred in sorted(bundle_eval["test"]["results"]):
        ok, rows = _compare(_point(runs, pred), bundle_eval["test"]["results"][pred]["point"])
        serving[pred] = {"match": ok, "metrics": rows}
        serving_ok &= ok
    serving_sec = time.perf_counter() - started

    # 2. a sample of test maps simulated again from their recorded seeds
    sample_maps = sorted({r.cluster for r in runs})[: args.maps]
    sample = [f for f in files if f.name.split("__")[0] in sample_maps]
    work = Path(args.work).resolve()
    work.mkdir(parents=True, exist_ok=True)
    tasks = []
    for f in sample:
        meta = json.loads(str(_load(f)["meta"]))
        tasks.append({"map_path": str(data / "maps" / f"{meta['map_id']}.json"), "npz_path": str(f),
                      "out_path": str(work / f.name), "scenario_seed": meta["scenario_seed"],
                      "steps": dataset["args"]["steps"], "snap_every": dataset["args"]["snapshot_every"],
                      "warmup": dataset["args"]["warmup"]})
    started = time.perf_counter()
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        resim = list(pool.map(_resimulate, tasks))
    stored_runs = _runs(sample, data, model, name, thresholds, v2_threshold)
    fresh_runs = _runs([work / f.name for f in sample], data, model, name, thresholds, v2_threshold)
    sample_cmp: dict[str, Any] = {}
    sample_ok = bool(resim) and all(r["truth_equal"] for r in resim)
    for pred in (name, "deterministic_cascade", "forecast_rule"):
        ok, rows = _compare(_point(fresh_runs, pred), _point(stored_runs, pred))
        sample_cmp[pred] = {"match": ok, "metrics": rows}
        sample_ok &= ok
    resim_sec = time.perf_counter() - started

    return {
        "model_version": model.model_version(),
        "dataset": {"path": str(data.relative_to(REPO)) if data.is_relative_to(REPO) else str(data),
                    "source_commit": dataset.get("source_commit"), "seed": dataset["args"]["seed"]},
        "serving_path": {"what": "every test-split snapshot through ML/cascade.py node_risk(), scored with "
                                 "ML/evaluation/cascade_eval.py and compared with eval.json test point metrics",
                         "runs": len(runs), "maps": len({r.cluster for r in runs}),
                         "tolerance": {"rate": TOL_RATE, "sec": TOL_SEC},
                         "seconds": round(serving_sec, 1), "passes": serving_ok, "predictors": serving},
        "resimulation": {"what": "sample test maps simulated again from their recorded seeds: ground truth "
                                 "and the deterministic cascade must be identical, and each predictor's metrics "
                                 "on the regenerated runs must match its metrics on the stored ones",
                         "note": "sensor readings / twin estimates differ slightly from the stored runs: the "
                                 "dataset was built before the twin stopped depending on PYTHONHASHSEED "
                                 "(reading order drew different noise per process); runs are now bit-identical "
                                 "across processes",
                         "maps": sample_maps, "runs": resim, "metrics": sample_cmp,
                         "seconds": round(resim_sec, 1), "passes": sample_ok},
        "passes": serving_ok and sample_ok,
    }


# --- shadow -------------------------------------------------------------------------------------
class ShadowEngine(HeadlessEngine):
    """The dataset's headless engine with the cascade model left on: with
    `gnn_mode: shadow` the cycle calls it through `call_ml` and scores it online,
    exactly as the live engine does."""

    def cascade_ml_mode(self) -> str:
        return Engine.cascade_ml_mode(self)


async def _shadow_run(label: str, scenario_seed: int, steps: int, warmup: int, disrupt: bool,
                      comparison) -> tuple[dict[str, np.ndarray], dict[str, Any]]:
    cfg = get_config().raw
    engine = ShadowEngine(GeneratedCity(build_topology(), cfg["events"]), scenario_seed)
    engine.prime_state()
    store = engine.store
    ids = list(store.nodes)
    n = len(ids)
    pos = {e: i for i, e in enumerate(ids)}
    crit = np.array([engine.critical_lines[e] for e in ids], dtype=np.float32)
    plan = _plan(random.Random(f"shadow:{label}:{scenario_seed}"), engine, steps) if disrupt else []
    applied: list[dict] = []
    online_cmp = OnlineEvaluator()
    cmp_threshold = comparison.alert_threshold(3600)
    rec: dict[str, list] = {k: [] for k in ("util", "det_eta", "model_p", "model_ttc", "model_ok",
                                            "cmp_p", "cmp_ttc", "cmp_ms")}
    truth = np.zeros((steps, n), dtype=np.float32)
    model_ms: list[float] = []
    import app.ml_registry as registry_mod

    real_call_ml = registry_mod.call_ml

    async def timed_call_ml(name, *a, **kw):   # latency of the model call inside the cycle
        t0 = time.perf_counter()
        out = await real_call_ml(name, *a, **kw)
        if name == "cascade.node_risk":
            model_ms.append((time.perf_counter() - t0) * 1000)
        return out

    import app.services.engine as engine_mod

    engine_mod.call_ml = timed_call_ml
    try:
        for k in range(steps):
            while plan and plan[0][0] == k:
                _, kind, params = plan.pop(0)
                if await _apply(engine, kind, params):
                    applied.append({"step": k, "kind": kind, "params": params})
            await engine.run_cycle()
            with engine.world_lock:
                u = engine.generator.utilisation()
            truth[k] = [u[e] for e in ids]
            ns = store.node_state_for_ml()
            det_eta = np.full(n, np.nan, dtype=np.float32)
            for c in store.cascades.values():
                for s in c["steps"]:
                    if s["predicted_band"] == "critical":
                        det_eta[pos[s["entity_id"]]] = np.nanmin([det_eta[pos[s["entity_id"]]], float(s["eta_sec"])])
            ml = store.cascade_ml or {}
            t0 = time.perf_counter()
            cmp_risk = comparison.node_risk(ns, store.edges, store.sim_time)
            rec["cmp_ms"].append((time.perf_counter() - t0) * 1000)
            for key_p, key_t, risk in (("model_p", "model_ttc", ml), ("cmp_p", "cmp_ttc", cmp_risk)):
                p = np.full((n, 3), np.nan, dtype=np.float32)
                t = np.full(n, np.nan, dtype=np.float32)
                for e, row in (risk.get("nodes") or {}).items():
                    p[pos[e]] = [row[f"p_fail_{h}"] for h in HORIZONS]
                    t[pos[e]] = np.nan if row.get("ttc_sec") is None else row["ttc_sec"]
                rec[key_p].append(p)
                rec[key_t].append(t)
            rec["model_ok"].append(ml.get("source") == "gnn")
            rec["util"].append([ns[e]["utilisation"] for e in ids])
            rec["det_eta"].append(det_eta)
            # the comparison model scored online under the engine's own definition
            scored = [e for e in ids if store.nodes[e]["entity_type"] in SCORED_TYPES]
            over = {e: float(store.entity_states[e]["utilisation"]) >= engine.critical_lines[e]
                    for e in scored if e in store.entity_states}
            online_cmp.update(store.cycle_number, engine.sim_dt, over, {"gnn": {
                e for e, row in (cmp_risk.get("nodes") or {}).items()
                if e in over and (row.get("p_fail_3600") or 0.0) >= cmp_threshold}})
    finally:
        engine_mod.call_ml = real_call_ml

    horizon_steps = HORIZONS[-1] // engine.sim_dt
    step_idx = np.arange(warmup, steps - horizon_steps, dtype=np.int32)
    ttc_true = np.full((len(step_idx), n), np.nan, dtype=np.float32)
    for s, k in enumerate(step_idx):
        future = truth[k + 1: k + 1 + horizon_steps] >= crit[None, :]
        hit = future.any(axis=0)
        ttc_true[s, hit] = (future.argmax(axis=0)[hit] + 1) * engine.sim_dt
    over = truth >= crit[None, :]
    cross_step, cross_ent = np.nonzero(over[1:] & ~over[:-1])
    saved = {
        "entity_ids": np.array(ids), "types": np.array([store.nodes[e]["entity_type"] for e in ids]),
        "critical": crit, "cycle_sec": np.int32(engine.sim_dt), "snapshot_step": step_idx,
        "util": np.array(rec["util"], dtype=np.float32)[step_idx],
        "det_eta": np.array(rec["det_eta"], dtype=np.float32)[step_idx],
        "model_p": np.array(rec["model_p"], dtype=np.float32)[step_idx],
        "model_ttc": np.array(rec["model_ttc"], dtype=np.float32)[step_idx],
        "model_ok": np.array(rec["model_ok"], dtype=bool),
        "cmp_p": np.array(rec["cmp_p"], dtype=np.float32)[step_idx],
        "cmp_ttc": np.array(rec["cmp_ttc"], dtype=np.float32)[step_idx],
        "ttc_true": ttc_true, "labels": np.stack([np.nan_to_num(ttc_true, nan=1e9) <= h for h in HORIZONS], axis=-1),
        "crossings": np.stack([cross_step + 1, cross_ent], axis=1).astype(np.int32),
    }
    q = lambda xs, p: round(float(np.percentile(xs, p)), 2) if xs else None  # noqa: E731
    info = {
        "label": label, "scenario_seed": scenario_seed, "steps": steps, "warmup": warmup, "disruptions": applied,
        "model_fallback_cycles": int((~saved["model_ok"]).sum()),
        "online": {"deterministic_cascade": store.online_eval.summary("deterministic_cascade"),
                   "model": store.online_eval.summary("gnn"), "comparison": online_cmp.summary("gnn")},
        "latency_ms": {"model_in_cycle": {"p50": q(model_ms, 50), "p95": q(model_ms, 95),
                                          "max": round(max(model_ms), 2) if model_ms else None},
                       "comparison": {"p50": q(rec["cmp_ms"], 50), "p95": q(rec["cmp_ms"], 95)},
                       "cascade_budget_ms": get_config().raw["budgets_ms"]["cascade"]},
    }
    return saved, info


def _shadow_runs_from_file(path: Path, names: tuple[str, str], thresholds: tuple[dict[int, float], dict[int, float]]):
    """The saved predictions -> an evaluation Run (this is what gets scored)."""
    npz = _load(path)
    mask = np.isin(npz["types"], np.array(SCORED_TYPES))[None, :] & (npz["util"] < npz["critical"][None, :])
    crossings = npz["crossings"]
    crossings = crossings[np.isin(npz["types"][crossings[:, 1]], np.array(SCORED_TYPES))] if len(crossings) else crossings
    det_eta = npz["det_eta"]
    det_alerts = np.stack([~np.isnan(det_eta) & (np.nan_to_num(det_eta, nan=1e9) <= h) for h in HORIZONS], axis=-1)
    preds = {"deterministic_cascade": {"scores": det_alerts.astype(float), "alerts": det_alerts, "ttc": det_eta}}
    for name, key, thr in ((names[0], "model", thresholds[0]), (names[1], "cmp", thresholds[1])):
        s = np.nan_to_num(npz[f"{key}_p"].astype(np.float64), nan=0.0)
        preds[name] = {"scores": s, "alerts": s >= np.array([thr[h] for h in HORIZONS])[None, None, :],
                       "ttc": npz[f"{key}_ttc"].astype(np.float64)}
    return ev.Run(cluster=path.stem, mask=mask, labels=npz["labels"], ttc_true=npz["ttc_true"],
                  snapshot_step=npz["snapshot_step"], crossings=crossings, cycle_sec=int(npz["cycle_sec"]),
                  predictions=preds)


def shadow(args: argparse.Namespace) -> dict[str, Any]:
    _unbounded_budgets()   # a slow machine must not turn the run into a fallback run; latency is reported instead
    model, comparison = predictor(args.bundle), predictor(args.compare)
    registry_cfg = get_config().raw["cascade"]
    registry_cfg["gnn_artifact"] = args.bundle   # the engine's own registry loads the model under test
    registry_cfg["gnn_mode"] = "shadow"
    names = (model.model_version().split("@")[0], comparison.model_version().split("@")[0])
    thresholds = ({h: model.alert_threshold(h) for h in HORIZONS}, {h: comparison.alert_threshold(h) for h in HORIZONS})
    out_dir = Path(args.save).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    seed = int(get_config().demo_seed)
    runs, infos = [], []
    for label, disrupt in (("demo", False), ("demo_disrupted", True)):
        started = time.perf_counter()
        saved, info = asyncio.run(_shadow_run(label, seed, args.steps, args.warmup, disrupt, comparison))
        path = out_dir / f"shadow_{names[0]}_{label}_seed{seed}.npz"
        np.savez_compressed(path, **saved)
        info["saved_predictions"] = str(path.relative_to(REPO)) if path.is_relative_to(REPO) else str(path)
        info["seconds"] = round(time.perf_counter() - started, 1)
        print(f"  {label}: {info['seconds']}s, {len(saved['crossings'])} crossings, "
              f"{info['model_fallback_cycles']} model fallback cycles", flush=True)
        runs.append(_shadow_runs_from_file(path, names, thresholds))
        infos.append(info)

    preds = ("deterministic_cascade", names[0], names[1])
    offline = {
        "per_run": {info["label"]: {p: ev._round(ev.point_metrics([r], p, probabilistic=p != preds[0])) for p in preds}
                    for info, r in zip(infos, runs)},
        "pooled": {p: ev._round(ev.point_metrics(runs, p, probabilistic=p != preds[0])) for p in preds},
    }
    base = build_topology()
    return {
        "model_version": model.model_version(), "comparison_version": comparison.model_version(),
        "map": "live", "live_topology_hash": topology_hash(base["nodes"], base["edges"]), "demo_seed": seed,
        "alert_thresholds": {names[0]: {str(h): t for h, t in thresholds[0].items()},
                             names[1]: {str(h): t for h, t in thresholds[1].items()},
                             "deterministic_cascade": "a published cascade step projected critical with eta <= h"},
        "definition": "offline: ML/evaluation/cascade_eval.py over every cycle after warm-up whose 3600 s "
                      "window lies inside the run; online: services/prediction_eval.py, as /metrics reports",
        "runs": infos, "offline": offline,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("reproduce")
    r.add_argument("--data", default=str(REPO / "data" / "cascade_v3_small"))
    r.add_argument("--bundle", default=V3_BUNDLE)
    r.add_argument("--maps", type=int, default=3, help="test maps to re-simulate (every scenario of each)")
    r.add_argument("--work", default=str(REPO / "data" / "reproduce"), help="where regenerated runs go")
    r.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    s = sub.add_parser("shadow")
    s.add_argument("--bundle", default=V3_BUNDLE)
    s.add_argument("--compare", default=V2_BUNDLE)
    s.add_argument("--steps", type=int, default=720, help="720 x 30 s = the whole 14:00-20:00 event")
    s.add_argument("--warmup", type=int, default=20)
    s.add_argument("--save", default=str(RESULTS), help="where the per-cycle predictions go")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("eventflow").setLevel(logging.ERROR)

    result = reproduce(args) if args.cmd == "reproduce" else shadow(args)
    name = load_manifest(REPO / args.bundle)["model_version"]
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / f"{name}_{args.cmd}.json"
    out.write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {out.relative_to(REPO)}  passes={result.get('passes', '-')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
