"""HX-Cascade v3 dataset: randomised maps, simulated by the real engine.

    cd Backend
    python -m scripts.cascade_dataset --out ../data/cascade_v3 \
        --maps-train 150 --maps-val 30 --maps-test 30 --scenarios 8 --live-scenarios 8 --workers 4

Why the real engine: the model must see at training time exactly what it sees
when serving. Each scenario runs `Engine` headless (no DB, cache, WebSocket or
interventions) on its own map, so every snapshot is `node_state_for_ml()` as
published live — sensor readings with noise and dropout, the EnKF twin's
estimate for unobserved entities, the twin-model forecast with its cached plan
projection. Labels come from the simulator's ground truth: did the entity reach
its critical line (config `thresholds`) within 900 / 1800 / 3600 s.

Recorded per snapshot, beside the model inputs, are the three baselines the
evaluation compares against, from the same serving code:
  * the published deterministic flow cascade (`services/cascade_flow.py`)
  * the forecaster's own threshold crossing (`time_to_critical_sec`)
  * HX-Cascade v2 (`node_risk`, the bundle in `cascade.gnn_artifact`)

Splits are by map: train / val / test maps are distinct random maps, and the
unmodified synthetic city ("live") is never randomised into any of them.

Output directory:
    dataset.json                 parameters, thresholds, code commit, counts, timing
    maps/<map_id>.json           topology + events + the operations that made it
    scenarios/<split>/<map_id>__s<k>.npz   one simulated run (see `_save`)
"""
from __future__ import annotations

import os

# Before any app import: this process never touches a real database.
os.environ.setdefault("DATABASE_URL", "sqlite://")

import argparse  # noqa: E402
import asyncio  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402
import math  # noqa: E402
import random  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
from concurrent.futures import ProcessPoolExecutor, as_completed  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any  # noqa: E402

import numpy as np  # noqa: E402

BACKEND = Path(__file__).resolve().parent.parent
REPO = BACKEND.parent
sys.path[:0] = [str(BACKEND), str(REPO)]

from app.config import get_config  # noqa: E402
from app.errors import ApiError  # noqa: E402
from app.providers.data import SyntheticData  # noqa: E402
from app.services.engine import Engine  # noqa: E402
from app.services.prediction_eval import SCORED_TYPES  # noqa: E402
from app.topology import build_topology  # noqa: E402
from ML.data.topology_randomiser import randomise  # noqa: E402

HORIZONS = (900, 1800, 3600)
log = logging.getLogger("eventflow.dataset")


# --- the engine, headless -------------------------------------------------------------------
class GeneratedCity(SyntheticData):
    """A data provider serving one fixed (randomised) map and schedule."""

    name = "generated"

    def __init__(self, topology: dict, events: list[dict]) -> None:
        self._topology = topology
        self._events = events

    def topology(self) -> dict:
        return json.loads(json.dumps(self._topology))

    def events(self, configured: list[dict]) -> list[dict]:
        return [dict(e) for e in self._events]


class HeadlessEngine(Engine):
    """The live cycle minus side effects and minus anything that would make the
    run depend on wall-clock speed (latency budgets, load shedding), so a seed
    reproduces the dataset on any machine."""

    def __init__(self, provider: Any, scenario_seed: int) -> None:
        self._scenario_seed = scenario_seed
        super().__init__(provider=provider, demo_checks=False)

    def _build_worlds(self) -> None:
        self.seed = self._scenario_seed   # sensor sampling and noise differ per scenario
        super()._build_worlds()

    def cascade_ml_mode(self) -> str:
        return "off"   # v2 is queried once per snapshot below, not every cycle

    async def _maybe_generate_interventions(self, node_state, sim_time):
        return []

    def _persist(self, sim_time: str) -> None:
        pass

    def _write_cache(self) -> None:
        pass

    async def _broadcast(self, *args, **kwargs) -> None:
        pass

    async def _broadcast_reconciled(self, *args, **kwargs) -> None:
        pass

    def _maybe_shed_load(self, total_ms: float) -> None:
        pass


def _unbounded_budgets() -> None:
    """Process-local: no ML call may fall back because this machine is slow."""
    budgets = get_config().raw["budgets_ms"]
    for k in budgets:
        budgets[k] = 600_000


# --- scenario plan: disruptions (unannounced) and schedule changes (announced) ---------------
def _plan(rng: random.Random, engine: Engine, steps: int) -> list[tuple[int, str, dict]]:
    types = engine.generator.types
    by = lambda t: sorted(e for e, x in types.items() if x == t)  # noqa: E731
    gates, stations, roads = by("gate"), by("transport_node"), by("road")
    events = sorted(engine.events.events)
    out: list[tuple[int, str, dict]] = []
    for _ in range(rng.randint(0, 4)):
        step = rng.randint(20, max(21, int(steps * 0.8)))
        kind = rng.choice(["gate_closure", "transport_outage", "attendance_delta", "weather_rain",
                           "road_capacity_delta", "metro_capacity_delta", "schedule_delay", "schedule_attendance"])
        if kind == "gate_closure" and gates:
            out.append((step, kind, {"entity_id": rng.choice(gates)}))
        elif kind == "transport_outage" and stations:
            out.append((step, kind, {"entity_id": rng.choice(stations)}))
        elif kind == "attendance_delta":
            out.append((step, kind, {"delta_pct": rng.uniform(-20, 35)}))
        elif kind == "weather_rain":
            out.append((step, kind, {"intensity": rng.choice(["moderate", "heavy"])}))
        elif kind == "road_capacity_delta" and roads:
            out.append((step, kind, {"entity_id": rng.choice(roads), "delta_pct": rng.uniform(-60, -20)}))
        elif kind == "metro_capacity_delta" and stations:
            out.append((step, kind, {"entity_id": rng.choice(stations), "delta_pct": rng.uniform(-50, -15)}))
        elif kind == "schedule_delay" and events:
            out.append((step, kind, {"event_id": rng.choice(events), "delay_sec": 60 * rng.randint(10, 45)}))
        elif kind == "schedule_attendance" and events:
            out.append((step, kind, {"event_id": rng.choice(events), "scale": rng.uniform(0.7, 1.4)}))
    return sorted(out, key=lambda x: x[0])


async def _apply(engine: Engine, kind: str, params: dict) -> bool:
    try:
        if kind == "schedule_delay":
            engine.update_event(params["event_id"], delay_sec=params["delay_sec"])
        elif kind == "schedule_attendance":
            ev = engine.events.get(params["event_id"])
            engine.update_event(params["event_id"],
                                expected_attendance=max(0, int(ev["expected_attendance"] * params["scale"])))
        else:
            engine.inject_disruption(kind, params)
    except ApiError:
        return False   # e.g. an event that already started cannot move; the live API refuses it too
    await engine.reconcile(kind)
    return True


# --- one simulated run -------------------------------------------------------------------------
async def _simulate(map_rec: dict, scenario_seed: int, steps: int, snap_every: int, warmup: int) -> dict:
    engine = HeadlessEngine(GeneratedCity(map_rec["topology"], map_rec["events"]), scenario_seed)
    engine.prime_state()
    for h in engine.store.history.values():
        h.clear()
    store = engine.store
    ids = list(store.nodes)
    n = len(ids)
    crit = np.array([engine.critical_lines[e] for e in ids], dtype=np.float32)
    v2 = engine.registry.cascade if getattr(engine.registry.cascade, "ready", lambda: False)() and \
        hasattr(engine.registry.cascade, "node_risk") else None
    horizon_steps = HORIZONS[-1] // engine.sim_dt
    rng = random.Random(f"plan:{map_rec['map_id']}:{scenario_seed}")
    plan = _plan(rng, engine, steps)
    applied: list[dict] = []

    truth = np.zeros((steps, n), dtype=np.float32)
    snaps: dict[str, list] = {k: [] for k in (
        "step", "util", "forecasts", "fc_ttc", "is_observed", "flow", "det_eta", "det_score", "v2_p", "v2_ttc")}
    started = time.perf_counter()
    for k in range(steps):
        while plan and plan[0][0] == k:
            _, kind, params = plan.pop(0)
            if await _apply(engine, kind, params):
                applied.append({"step": k, "kind": kind, "params": params})
        await engine.run_cycle()
        with engine.world_lock:
            u = engine.generator.utilisation()
        truth[k] = [u[e] for e in ids]
        if k < warmup or k % snap_every or k + horizon_steps >= steps:
            continue
        ns = store.node_state_for_ml()
        det_eta = np.full(n, np.nan, dtype=np.float32)
        det_score = np.zeros(n, dtype=np.float32)
        pos = {e: i for i, e in enumerate(ids)}
        for c in store.cascades.values():
            for s in c["steps"]:
                i = pos[s["entity_id"]]
                det_score[i] = max(det_score[i], float(s["failure_probability"]))
                if s["predicted_band"] == "critical":
                    det_eta[i] = np.nanmin([det_eta[i], float(s["eta_sec"])])
        v2_p = np.full((n, 3), np.nan, dtype=np.float32)
        v2_ttc = np.full(n, np.nan, dtype=np.float32)
        if v2 is not None:
            risk = v2.node_risk(ns, store.edges, store.sim_time)
            for e, row in (risk.get("nodes") or {}).items():
                v2_p[pos[e]] = [row[f"p_fail_{h}"] for h in HORIZONS]
                v2_ttc[pos[e]] = row["ttc_sec"]
        nan = lambda v: np.nan if v is None else float(v)  # noqa: E731
        snaps["step"].append(k)
        snaps["util"].append([ns[e]["utilisation"] for e in ids])
        snaps["forecasts"].append([[nan(ns[e][f"forecast_{h}"]) for h in HORIZONS] for e in ids])
        snaps["fc_ttc"].append([nan(ns[e]["time_to_critical_sec"]) for e in ids])
        snaps["is_observed"].append([ns[e]["is_observed"] for e in ids])
        snaps["flow"].append([ns[e]["flow_rate_per_min"] for e in ids])
        snaps["det_eta"].append(det_eta)
        snaps["det_score"].append(det_score)
        snaps["v2_p"].append(v2_p)
        snaps["v2_ttc"].append(v2_ttc)
    elapsed = time.perf_counter() - started

    step_idx = np.array(snaps["step"], dtype=np.int32)
    # Labels from ground truth: first step after the snapshot at/over the critical line.
    ttc_true = np.full((len(step_idx), n), np.nan, dtype=np.float32)
    for s, k in enumerate(step_idx):
        future = truth[k + 1: k + 1 + horizon_steps] >= crit[None, :]
        hit = future.any(axis=0)
        first = future.argmax(axis=0)
        ttc_true[s, hit] = (first[hit] + 1) * engine.sim_dt
    labels = np.stack([np.nan_to_num(ttc_true, nan=1e9) <= h for h in HORIZONS], axis=-1)
    over = truth >= crit[None, :]
    cross_step, cross_ent = np.nonzero(over[1:] & ~over[:-1])
    return {
        "entity_ids": np.array(ids), "types": np.array([store.nodes[e]["entity_type"] for e in ids]),
        "capacity": np.array([store.nodes[e]["nominal_capacity"] for e in ids], dtype=np.float32),
        "critical": crit, "cycle_sec": np.int32(engine.sim_dt), "snapshot_step": step_idx,
        "util": np.array(snaps["util"], dtype=np.float32),
        "forecasts": np.array(snaps["forecasts"], dtype=np.float32),
        "fc_ttc": np.array(snaps["fc_ttc"], dtype=np.float32),
        "is_observed": np.array(snaps["is_observed"], dtype=bool),
        "flow": np.array(snaps["flow"], dtype=np.float32),
        "det_eta": np.array(snaps["det_eta"], dtype=np.float32),
        "det_score": np.array(snaps["det_score"], dtype=np.float32),
        "v2_p": np.array(snaps["v2_p"], dtype=np.float32), "v2_ttc": np.array(snaps["v2_ttc"], dtype=np.float32),
        "truth_over_at_snapshot": over[step_idx], "ttc_true": ttc_true, "labels": labels,
        "crossings": np.stack([cross_step + 1, cross_ent], axis=1).astype(np.int32),
        "_meta": {"applied": applied, "seconds": round(elapsed, 2), "steps": steps,
                  "v2_model_version": v2.model_version() if v2 is not None else None},
    }


def _run_task(task: dict) -> dict:
    logging.basicConfig(level=logging.ERROR)
    logging.getLogger().setLevel(logging.ERROR)
    _unbounded_budgets()
    map_rec = json.loads(Path(task["map_path"]).read_text(encoding="utf-8"))
    result = asyncio.run(_simulate(map_rec, task["scenario_seed"], task["steps"], task["snap_every"], task["warmup"]))
    meta = result.pop("_meta")
    out = Path(task["out_path"])
    out.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(out, **result, meta=np.array(json.dumps({
        **meta, "map_id": map_rec["map_id"], "split": task["split"], "scenario_seed": task["scenario_seed"]})))
    return {"out": str(out), "seconds": meta["seconds"], "snapshots": int(len(result["snapshot_step"])),
            "positives_3600": int(result["labels"][..., 2].sum()), "crossings": int(len(result["crossings"]))}


# --- driver -------------------------------------------------------------------------------------
def _git_commit() -> str | None:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True,
                              check=True).stdout.strip()
    except Exception:
        return None


async def _smoke_cycles(map_rec: dict, cycles: int) -> None:
    engine = HeadlessEngine(GeneratedCity(map_rec["topology"], map_rec["events"]), scenario_seed=0)
    engine.prime_state()
    for _ in range(cycles):
        await engine.run_cycle()


def runnable(map_rec: dict, cycles: int = 3) -> str | None:
    """Why the simulator cannot run this map (None = it can): the randomiser's
    graph checks are necessary, this is the sufficient one."""
    try:
        asyncio.run(_smoke_cycles(map_rec, cycles))
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    return None


def _git_dirty() -> bool | None:
    """True when the code that made the dataset differs from `source_commit`."""
    try:
        out = subprocess.run(["git", "status", "--porcelain", "--", "Backend", "ML"], cwd=REPO, capture_output=True,
                             text=True, check=True).stdout
        return any(not line.startswith("??") or line[3:].endswith(".py") for line in out.splitlines())
    except Exception:
        return None


def build_maps(out: Path, counts: dict[str, int], seed: int) -> list[tuple[str, Path]]:
    cfg = get_config().raw
    base, events = build_topology(), cfg["events"]
    primary = cfg["event"]["event_id"]
    maps_dir = out / "maps"
    maps_dir.mkdir(parents=True, exist_ok=True)
    made: list[tuple[str, Path]] = []
    from ML.manifest import topology_hash

    live_hash = topology_hash(base["nodes"], base["edges"])
    live = {"map_id": "live", "split": "live", "topology": base, "events": events, "seed": None, "ops": [],
            "topology_hash": live_hash}
    (maps_dir / "live.json").write_text(json.dumps(live), encoding="utf-8")
    made.append(("live", maps_dir / "live.json"))
    seen = {live_hash}
    rejected: list[dict] = []
    offset = 0
    for split in ("train", "val", "test"):
        for i in range(counts[split]):
            while True:
                rec = randomise(base, events, seed=seed * 1_000_003 + offset, primary_event_id=primary)
                offset += 1
                if rec["topology_hash"] in seen:
                    continue
                why = runnable(rec)
                if why is None:
                    break
                rejected.append({"seed": rec["seed"], "ops": rec["ops"], "reason": why})
                log.warning("map seed %s rejected: %s", rec["seed"], why)
            seen.add(rec["topology_hash"])
            rec["map_id"] = f"{split}_{i:04d}"
            rec["split"] = split
            path = maps_dir / f"{rec['map_id']}.json"
            path.write_text(json.dumps(rec), encoding="utf-8")
            made.append((split, path))
    (maps_dir / "rejected.json").write_text(json.dumps(rejected, indent=2), encoding="utf-8")
    return made


def _library_versions() -> dict[str, str]:
    import platform

    return {"python": platform.python_version(), "numpy": np.__version__}


def _summary(out: Path, args: argparse.Namespace, maps: list[tuple[str, Path]], extra: dict) -> dict:
    """dataset.json. Written before simulating and again after, and it counts the
    runs actually on disk, so a session cut off mid-way still leaves a usable,
    honestly described dataset."""
    cfg = get_config().raw
    return {
        "created_by": "Backend/scripts/cascade_dataset.py", "source_commit": _git_commit(),
        "source_dirty": _git_dirty(),
        "args": {k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()},
        "horizons_sec": list(HORIZONS), "scored_types": list(SCORED_TYPES),
        "thresholds": cfg["thresholds"], "cycle_sec": cfg["cycle_sec"],
        "maps": {s: sum(1 for x, _ in maps if x == s) for s in ("train", "val", "test", "live")},
        "maps_rejected_by_simulator": len(json.loads((out / "maps" / "rejected.json").read_text(encoding="utf-8"))),
        "runs": {s: len(list((out / "scenarios" / s).glob("*.npz"))) for s in ("train", "val", "test", "live")},
        "library_versions": _library_versions(),
        **extra,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--maps-train", type=int, default=150)
    ap.add_argument("--maps-val", type=int, default=30)
    ap.add_argument("--maps-test", type=int, default=30)
    ap.add_argument("--scenarios", type=int, default=8, help="simulated runs per random map")
    ap.add_argument("--live-scenarios", type=int, default=8, help="runs on the unmodified live map (test only)")
    ap.add_argument("--steps", type=int, default=720, help="cycles per run (720 x 30 s = 14:00-20:00)")
    ap.add_argument("--snapshot-every", type=int, default=10, help="cycles between recorded snapshots")
    ap.add_argument("--warmup", type=int, default=20, help="cycles before the first snapshot")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    ap.add_argument("--time-budget-min", type=float, default=None,
                    help="stop starting new runs after this many minutes (running ones finish); re-run to resume")
    args = ap.parse_args()

    out: Path = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    # Resuming is only safe with the same map plan: map ids are positional, so a
    # different count or seed would pair saved runs with different maps.
    previous = out / "dataset.json"
    if previous.exists():
        old = json.loads(previous.read_text(encoding="utf-8")).get("args", {})
        keys = ("maps_train", "maps_val", "maps_test", "seed", "steps", "snapshot_every", "warmup")
        changed = {k: (old.get(k), getattr(args, k)) for k in keys if old.get(k) != getattr(args, k)}
        if changed:
            raise SystemExit(f"{out} was made with different settings {changed}; use a new --out directory")
    counts = {"train": args.maps_train, "val": args.maps_val, "test": args.maps_test}
    maps = build_maps(out, counts, args.seed)

    # Order: the evaluation splits first (live, test, val), then train maps one
    # scenario index at a time. A run cut short by the time budget then costs
    # training breadth only, never a held-out split.
    order = {"live": 0, "test": 1, "val": 2, "train": 3}
    planned = []
    for split, path in maps:
        runs = args.live_scenarios if split == "live" else args.scenarios
        for k in range(runs):
            planned.append((order[split], k if split == "train" else 0, split, path, k))
    planned.sort(key=lambda t: (t[0], t[1], t[3].stem, t[4]))
    tasks = []
    for _, _, split, path, k in planned:
        out_path = out / "scenarios" / split / f"{path.stem}__s{k}.npz"
        if out_path.exists():
            continue   # resumable: a session that stopped picks up where it left off
        tasks.append({"map_path": str(path), "split": split, "scenario_seed": args.seed * 10_007 + 97 * k + 1,
                      "steps": args.steps, "snap_every": args.snapshot_every, "warmup": args.warmup,
                      "out_path": str(out_path)})
    (out / "dataset.json").write_text(json.dumps(_summary(out, args, maps, {"complete": False}), indent=2),
                                      encoding="utf-8")
    print(f"{len(maps)} maps, {len(tasks)} runs to simulate with {args.workers} workers", flush=True)

    started = time.perf_counter()
    deadline = started + args.time_budget_min * 60 if args.time_budget_min else None
    results: list[dict] = []
    stopped_early = False
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        pending = iter(tasks)
        running = set()
        # Keep exactly `workers` runs in flight, so the budget check happens
        # before each new run starts rather than after everything is queued.
        for t in pending:
            running.add(pool.submit(_run_task, t))
            if len(running) >= args.workers:
                break
        while running:
            done = next(as_completed(running))
            running.remove(done)
            results.append(done.result())
            n = len(results)
            if n % max(1, len(tasks) // 20) == 0 or n == len(tasks):
                elapsed = time.perf_counter() - started
                eta = elapsed / n * (len(tasks) - n)
                print(f"  {n}/{len(tasks)} runs  ({elapsed / 60:.0f} min elapsed, ~{eta / 60:.0f} min left)", flush=True)
            if deadline and time.perf_counter() > deadline:
                stopped_early = stopped_early or next(pending, None) is not None
                continue
            nxt = next(pending, None)
            if nxt is not None:
                running.add(pool.submit(_run_task, nxt))
    wall = time.perf_counter() - started

    per_run = [r["seconds"] for r in results]
    summary = _summary(out, args, maps, {
        "complete": not stopped_early and len(results) == len(tasks),
        "runs_this_invocation": len(results),
        "timing": {"wall_sec": round(wall, 1), "mean_run_sec": round(sum(per_run) / len(per_run), 2) if per_run else None,
                   "workers": args.workers},
    })
    (out / "dataset.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps({k: summary[k] for k in ("complete", "runs", "timing")}), flush=True)
    if stopped_early:
        print("time budget reached: re-run the same command to simulate the remaining runs", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
