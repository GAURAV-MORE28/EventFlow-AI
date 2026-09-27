"""Forecast-correction dataset: (features, twin-model forecast, actual) from simulated runs.

    cd Backend
    python -m scripts.forecast_dataset --out ../data/forecast_v2 --train 60 --val 15 --test 30 --workers 4

Every run is the real engine, headless (`scripts/cascade_dataset.HeadlessEngine`),
on one of the randomised maps of the cascade dataset (`--maps`, default
../data/cascade_v3/maps; its train / val / test split is kept, so a map is in
one split only) with that dataset's seeded disruption plan. Two more runs on the
unmodified live map are evaluation-only: `demo` (the plain event, seed 42, the
03 §2.5 "demo scenario") and `demo_disrupted` (the same event with the seeded
disruption plan of `scripts/cascade_swap_eval.py shadow`).

Each cycle the forecaster's own inputs and output are captured
(`LoggingEngine._predict_with_prior`), and every `--snap-every` cycles one row
per entity on the twin-model path is written:

    X         ML/forecast_correction.features(...) of exactly those inputs
    twin      the twin-model forecast at 900 / 1800 / 3600 s (what was published)
    twin_path the dense twin-model forecast its time_to_critical_sec was found on (t = 0, 60, ... 3600 s)
    twin_ttc  its time_to_critical_sec (NaN = none within the horizon)
    actual    the simulator's true utilisation at t + h      (ground truth)
    observed  the published reading at t + h                 (what /metrics sees)
    ttc_true  first true crossing of the entity's critical line after t (NaN = none within 3600 s)

The correction model is switched off while logging, so `twin` is always the
uncorrected twin model.
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
from concurrent.futures import ProcessPoolExecutor, as_completed  # noqa: E402
from pathlib import Path  # noqa: E402

import numpy as np  # noqa: E402

from scripts.cascade_dataset import (  # noqa: E402  (also puts Backend/ and the repo on sys.path)
    REPO, GeneratedCity, HeadlessEngine, _apply, _git_commit, _git_dirty, _plan, _unbounded_budgets,
)
from app.config import get_config  # noqa: E402
from app.topology import build_topology  # noqa: E402
from ML.forecast_correction import FEATURE_NAMES, HORIZONS, features  # noqa: E402

log = logging.getLogger("eventflow.forecast_dataset")


class LoggingEngine(HeadlessEngine):
    """Keeps the last forecaster call: its inputs and its output."""

    last_call: tuple | None = None

    def _predict_with_prior(self, series, capacities, horizons, sim_time, prior):
        out = super()._predict_with_prior(series, capacities, horizons, sim_time, prior)
        self.last_call = (series, capacities, prior, out, horizons, sim_time)
        return out


async def simulate(topology: dict, events: list[dict], scenario_seed: int, plan_rng: random.Random | None,
                   steps: int, snap_every: int, warmup: int) -> dict[str, np.ndarray]:
    engine = LoggingEngine(GeneratedCity(topology, events), scenario_seed)
    engine.prime_state()
    for h in engine.store.history.values():
        h.clear()
    store = engine.store
    fc = engine.registry.forecaster
    ids = list(store.nodes)
    n = len(ids)
    crit = np.array([fc.critical_by_entity.get(e, fc.critical) for e in ids], dtype=np.float32)
    dt = engine.sim_dt
    ahead = HORIZONS[-1] // dt
    plan = _plan(plan_rng, engine, steps) if plan_rng is not None else []

    truth = np.zeros((steps, n), dtype=np.float32)
    observed = np.zeros((steps, n), dtype=np.float32)
    rows: dict[str, list] = {k: [] for k in ("X", "twin", "twin_ttc", "twin_path", "step", "entity")}
    for k in range(steps):
        while plan and plan[0][0] == k:
            _, kind, params = plan.pop(0)
            await _apply(engine, kind, params)
        engine.last_call = None
        await engine.run_cycle()
        with engine.world_lock:
            u = engine.generator.utilisation()
        truth[k] = [u[e] for e in ids]
        observed[k] = [store.entity_states[e]["utilisation"] for e in ids]
        if k < warmup or k % snap_every or k + ahead >= steps or engine.last_call is None:
            continue
        series, capacities, prior, out, horizons, sim_time = engine.last_call
        for i, e in enumerate(ids):
            f = (out or {}).get(e)
            if not f or f.get("source") != "twin_model" or not prior or e not in prior:
                continue
            twin = {p["horizon_sec"]: p["predicted_utilisation"] for p in f["points"]}
            aligned = fc.aligned_prior(prior[e], sim_time, horizons)   # what the forecaster used
            rows["X"].append(features(series[e], capacities.get(e, 0.0), float(crit[i]), aligned, twin))
            rows["twin_path"].append([v for _, v in fc._forecast_model(e, series[e], aligned, horizons, sim_time)[1]])
            rows["twin"].append([twin[h] for h in HORIZONS])
            ttc = f.get("time_to_critical_sec")
            rows["twin_ttc"].append(np.nan if ttc is None else float(ttc))
            rows["step"].append(k)
            rows["entity"].append(i)

    step = np.array(rows["step"], dtype=np.int32)
    ent = np.array(rows["entity"], dtype=np.int32)
    actual = np.stack([truth[step + h // dt, ent] for h in HORIZONS], axis=1)
    obs_ahead = np.stack([observed[step + h // dt, ent] for h in HORIZONS], axis=1)
    over_now = truth[step, ent] >= crit[ent]
    ttc_true = np.full(len(step), np.nan, dtype=np.float32)
    for r, (k, i) in enumerate(zip(step, ent)):
        hit = np.nonzero(truth[k + 1: k + 1 + ahead, i] >= crit[i])[0]
        if hit.size:
            ttc_true[r] = (hit[0] + 1) * dt
    return {
        "X": np.array(rows["X"], dtype=np.float32), "twin": np.array(rows["twin"], dtype=np.float32),
        "twin_ttc": np.array(rows["twin_ttc"], dtype=np.float32),
        "twin_path": np.array(rows["twin_path"], dtype=np.float32), "path_step_sec": np.int32(60),
        "actual": actual, "observed": obs_ahead,
        "ttc_true": ttc_true, "over_now": over_now, "step": step, "entity": ent,
        "entity_ids": np.array(ids), "types": np.array([store.nodes[e]["entity_type"] for e in ids]),
        "critical": crit, "cycle_sec": np.int32(dt),
    }


def run_task(task: dict) -> dict:
    logging.basicConfig(level=logging.ERROR)
    logging.getLogger().setLevel(logging.ERROR)
    _unbounded_budgets()
    get_config().raw["forecaster"]["correction_artifact"] = None   # log the uncorrected twin model
    started = time.perf_counter()
    if task["split"] == "demo":
        topology, events = build_topology(), get_config().raw["events"]
        rng = random.Random(f"shadow:demo_disrupted:{task['seed']}") if task["disrupt"] else None
    else:
        rec = json.loads(Path(task["map_path"]).read_text(encoding="utf-8"))
        topology, events = rec["topology"], rec["events"]
        rng = random.Random(f"plan:{rec['map_id']}:{task['seed']}")
    out = asyncio.run(simulate(topology, events, task["seed"], rng, task["steps"], task["snap_every"],
                               task["warmup"]))
    path = Path(task["out_path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    # Written aside and renamed: a crashed worker must not leave a file that
    # looks finished (a rerun skips every run whose file exists).
    tmp = path.with_name(path.stem + ".partial.npz")
    np.savez_compressed(tmp, **out, meta=np.array(json.dumps({k: task[k] for k in ("name", "split", "seed")})))
    tmp.replace(path)
    return {"out": str(path), "rows": int(len(out["X"])), "seconds": round(time.perf_counter() - started, 1)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(REPO / "data" / "forecast_v2"))
    ap.add_argument("--maps", default=str(REPO / "data" / "cascade_v3" / "maps"))
    ap.add_argument("--train", type=int, default=60, help="train maps used")
    ap.add_argument("--val", type=int, default=15)
    ap.add_argument("--test", type=int, default=30)
    ap.add_argument("--scenarios", type=int, default=1, help="seeded scenarios per map")
    ap.add_argument("--steps", type=int, default=720)
    ap.add_argument("--snap-every", type=int, default=5)
    ap.add_argument("--warmup", type=int, default=20)
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    out, maps = Path(args.out), Path(args.maps)
    common = {"steps": args.steps, "snap_every": args.snap_every, "warmup": args.warmup}
    tasks = [{"name": name, "split": "demo", "seed": 42, "disrupt": disrupt, "map_path": None,
              "out_path": str(out / "demo" / f"{name}.npz"), **common}
             for name, disrupt in (("demo", False), ("demo_disrupted", True))]
    for split, count in (("train", args.train), ("val", args.val), ("test", args.test)):
        for path in sorted(maps.glob(f"{split}_*.json"))[:count]:
            for s in range(args.scenarios):
                name = f"{path.stem}__s{s}"
                tasks.append({"name": name, "split": split, "seed": s, "disrupt": True, "map_path": str(path),
                              "out_path": str(out / split / f"{name}.npz"), **common})
    todo = [t for t in tasks if not Path(t["out_path"]).exists()]
    print(f"{len(tasks)} runs ({len(tasks) - len(todo)} already done), {args.workers} workers", flush=True)
    started = time.perf_counter()
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(run_task, t): t for t in todo}
        for i, fut in enumerate(as_completed(futures), 1):
            r = fut.result()
            print(f"[{i}/{len(todo)}] {Path(r['out']).stem}: {r['rows']} rows, {r['seconds']}s", flush=True)
    (out / "dataset.json").write_text(json.dumps({
        "created_by": "Backend/scripts/forecast_dataset.py", "args": vars(args),
        "feature_names": list(FEATURE_NAMES), "horizons_sec": list(HORIZONS),
        "source_commit": _git_commit(), "source_dirty": _git_dirty(),
        "runs": {s: sum(1 for t in tasks if t["split"] == s) for s in ("train", "val", "test", "demo")},
        "seconds": round(time.perf_counter() - started, 1),
    }, indent=1) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
