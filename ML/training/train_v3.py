"""Train, calibrate and evaluate HX-Cascade v3; write a verified bundle.

    python -m ML.training.train_v3 --data data/cascade_v3 --out ML/artifacts/hx_cascade_v3

(from the repository root). Reads the dataset written by
`Backend/scripts/cascade_dataset.py`; imports nothing from the backend.

  1. features  — ML/features/graph_features.py, the same builder serving uses
  2. train     — train maps; early stopping on validation-map loss; with
                 --sweep every config is trained and the winner is chosen on
                 validation maps only (mean AP over the three horizons)
  3. calibrate — one temperature per horizon, fitted on validation maps (NLL)
  4. threshold — alert threshold per horizon maximising F1 on validation maps
  5. OOD stats — feature ranges and embedding distribution on train maps
                 (ood_stats.json)
  6. evaluate  — test maps and the live map (never used above), against the
                 deterministic cascade, the forecast rule and v2, with
                 cluster-bootstrap intervals (ML/evaluation/cascade_eval.py)
  7. bundle    — model.pt, feature_norm.json, calibration.json, ood_stats.json,
                 eval.json, training.json, manifest.json (every file sha256-bound;
                 commit, dataset version, seeds, library versions)
"""
from __future__ import annotations

import argparse
import json
import math
import subprocess
import time
from pathlib import Path
from typing import Any

import numpy as np
import torch
import torch.nn.functional as F
from torch_geometric.data import Data
from torch_geometric.loader import DataLoader

from ..evaluation import cascade_eval as ev
from ..features import graph_features as gf
from ..manifest import write_manifest
from ..models.hx_cascade import HXCascadeV3

REPO = Path(__file__).resolve().parents[2]


# --- data ------------------------------------------------------------------------------------
def _load_npz(path: Path) -> dict[str, np.ndarray]:
    with np.load(path, allow_pickle=False) as d:
        return {k: d[k] for k in d.files}


class Split:
    """All runs of one split: graphs for training plus arrays for evaluation."""

    def __init__(self, data_dir: Path, split: str, scored: tuple[str, ...]):
        self.name = split
        self.runs: list[dict[str, Any]] = []
        maps: dict[str, dict] = {}
        for f in sorted((data_dir / "scenarios" / split).glob("*.npz")):
            npz = _load_npz(f)
            meta = json.loads(str(npz.pop("meta")))
            map_id = meta["map_id"]
            if map_id not in maps:
                maps[map_id] = json.loads((data_dir / "maps" / f"{map_id}.json").read_text(encoding="utf-8"))
            ids = [str(x) for x in npz["entity_ids"]]
            types = [str(x) for x in npz["types"]]
            graph = gf.static_graph(ids, types, maps[map_id]["topology"]["edges"])
            self.runs.append({"file": f.name, "map_id": map_id, "meta": meta, "npz": npz, "types": types,
                              "graph": graph, "mask": ev.sample_mask(npz, scored),
                              "crossings": ev.scored_crossings(npz, scored)})

    def graphs(self) -> list[Data]:
        out = []
        for r in self.runs:
            npz, graph = r["npz"], r["graph"]
            ei = torch.from_numpy(graph["edge_index"])
            ea = torch.from_numpy(graph["edge_attr"])
            for s in range(len(npz["snapshot_step"])):
                x = gf.node_features(npz["util"][s], npz["forecasts"][s], npz["capacity"], r["types"],
                                     npz["critical"], npz["is_observed"][s], npz["flow"][s], graph)
                out.append(Data(
                    x=torch.from_numpy(x), edge_index=ei, edge_attr=ea,
                    y=torch.from_numpy(npz["labels"][s].astype(np.float32)),
                    ttc=torch.from_numpy(np.nan_to_num(npz["ttc_true"][s], nan=0.0)),
                    mask=torch.from_numpy(r["mask"][s]),
                ))
        return out


# --- model training -------------------------------------------------------------------------
def _loss(out: dict, batch: Data, pos_weight: torch.Tensor, ttc_weight: float) -> torch.Tensor:
    m = batch.mask
    logits, y = out["failure_logits"][m], batch.y[m]
    loss = F.binary_cross_entropy_with_logits(logits, y, pos_weight=pos_weight)
    hit = y[:, 2] > 0.5
    if hit.any():
        loss = loss + ttc_weight * F.l1_loss(out["ttc"][m][hit] / 3600.0, batch.ttc[m][hit] / 3600.0)
    return loss


@torch.no_grad()
def _predict(model: HXCascadeV3, graphs: list[Data], device: torch.device,
             batch_size: int = 64) -> list[dict[str, np.ndarray]]:
    model.eval()
    out = []
    for batch in DataLoader(graphs, batch_size=batch_size, shuffle=False):
        batch = batch.to(device)
        o = {k: v.cpu() for k, v in model(batch.x, batch.edge_index, batch.edge_attr).items()}
        which = batch.batch.cpu()
        for i in range(batch.num_graphs):
            sel = which == i
            out.append({"logits": o["failure_logits"][sel].numpy(), "ttc": o["ttc"][sel].numpy(),
                        "embedding": o["embedding"][sel].numpy()})
    return out


POS_WEIGHT_MODES = ("sqrt", "linear", "none")


def _pos_weight(train_graphs: list[Data], mode: str) -> torch.Tensor:
    """Class weighting for the rare positives; calibration afterwards undoes its
    effect on probabilities, so it only changes what the network learns."""
    y = torch.cat([g.y[g.mask] for g in train_graphs])
    pos = y.mean(dim=0).clamp(min=1e-4)
    if mode == "sqrt":
        return torch.sqrt((1 - pos) / pos).clamp(max=10.0)
    if mode == "linear":
        return ((1 - pos) / pos).clamp(max=50.0)
    if mode == "none":
        return torch.ones_like(pos)
    raise ValueError(f"pos_weight must be one of {POS_WEIGHT_MODES}, not {mode!r}")


def train(train_graphs: list[Data], val_graphs: list[Data], cfg: dict[str, Any], args: argparse.Namespace,
          device: torch.device) -> tuple[HXCascadeV3, dict]:
    """Train one configuration (`cfg`: hidden, layers, heads, dropout, lr,
    batch_size, pos_weight); early stopping on validation-map loss."""
    torch.manual_seed(args.seed)
    model = HXCascadeV3(gf.NODE_FEAT_DIM, gf.EDGE_FEAT_DIM, hidden=cfg["hidden"], num_layers=cfg["layers"],
                        heads=cfg["heads"], dropout=cfg["dropout"]).to(device)
    pos_weight = _pos_weight(train_graphs, cfg["pos_weight"]).to(device)
    opt = torch.optim.AdamW(model.parameters(), lr=cfg["lr"], weight_decay=1e-4)
    gen = torch.Generator().manual_seed(args.seed)
    best, best_state, history, stale = math.inf, None, [], 0
    for epoch in range(args.epochs):
        epoch_started = time.perf_counter()
        model.train()
        total = 0.0
        for batch in DataLoader(train_graphs, batch_size=cfg["batch_size"], shuffle=True, generator=gen):
            batch = batch.to(device)
            loss = _loss(model(batch.x, batch.edge_index, batch.edge_attr), batch, pos_weight, args.ttc_weight)
            opt.zero_grad()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            opt.step()
            total += loss.item() * batch.num_graphs
        model.eval()
        with torch.no_grad():
            val = sum(float(_loss(model(b.x, b.edge_index, b.edge_attr), b, pos_weight, args.ttc_weight)) * b.num_graphs
                      for b in (x.to(device) for x in DataLoader(val_graphs, batch_size=256))) / max(len(val_graphs), 1)
        history.append({"epoch": epoch + 1, "train_loss": round(total / max(len(train_graphs), 1), 5),
                        "val_loss": round(val, 5), "seconds": round(time.perf_counter() - epoch_started, 2)})
        print(f"  epoch {epoch + 1}: train {history[-1]['train_loss']:.4f}  val {val:.4f}  "
              f"({history[-1]['seconds']:.0f}s)", flush=True)
        if val < best - 1e-4:
            best, stale = val, 0
            best_state = {k: v.detach().clone() for k, v in model.state_dict().items()}
        else:
            stale += 1
            if stale >= args.patience:
                break
    if best_state is not None:
        model.load_state_dict(best_state)
    model = model.cpu()   # evaluation and the saved bundle are CPU, like serving
    return model, {"history": history, "best_val_loss": best,
                   "pos_weight": [round(float(w), 3) for w in pos_weight]}


def selection_score(preds: list[dict], graphs: list[Data]) -> float:
    """Model selection metric, validation maps only: mean average precision over
    the three horizons. AP ranks, so it is unaffected by class weighting and by
    the temperature fitted afterwards, which makes it comparable across configs."""
    mask = np.concatenate([g.mask.numpy() for g in graphs])
    logits = np.concatenate([p["logits"] for p in preds])[mask]
    labels = np.concatenate([g.y.numpy() for g in graphs])[mask] > 0.5
    aps = [ev.average_precision(logits[:, j], labels[:, j]) for j in range(3)]
    aps = [a for a in aps if a == a]
    return float(np.mean(aps)) if aps else float("nan")


# --- calibration, thresholds, OOD ---------------------------------------------------------------
TEMPERATURE_RANGE = (0.05, 20.0)


def _nll(z: np.ndarray, y: np.ndarray) -> float:
    """Mean binary cross-entropy of logits z against labels y, numerically stable."""
    return float(np.mean(np.logaddexp(0.0, z) - y * z))


def fit_temperatures(logits: np.ndarray, labels: np.ndarray) -> list[float]:
    """One temperature per horizon minimising validation NLL, by a bounded 1-D
    search over log T (a coarse grid, then a fine one around the best point).
    Bounded and deterministic: an unbounded optimiser can run T to 0 or to
    infinity when the logits are poor. A horizon with only one class keeps T = 1."""
    lo, hi = np.log(TEMPERATURE_RANGE[0]), np.log(TEMPERATURE_RANGE[1])
    temps = []
    for j in range(logits.shape[1]):
        z, y = logits[:, j].astype(np.float64), labels[:, j].astype(np.float64)
        if y.sum() == 0 or y.sum() == len(y):
            temps.append(1.0)
            continue
        grid = np.linspace(lo, hi, 121)
        best = grid[int(np.argmin([_nll(z / np.exp(g), y) for g in grid]))]
        step = grid[1] - grid[0]
        fine = np.linspace(max(lo, best - step), min(hi, best + step), 41)
        best = fine[int(np.argmin([_nll(z / np.exp(g), y) for g in fine]))]
        temps.append(round(float(np.exp(best)), 4))
    return temps


def calibrated(logits: np.ndarray, temps: list[float]) -> np.ndarray:
    z = logits / np.array(temps)[None, :]
    p = 0.5 * (1.0 + np.tanh(0.5 * z))   # sigmoid without overflow
    return np.maximum.accumulate(p, axis=-1)   # per-horizon temperatures must not break p900 <= p1800 <= p3600


def choose_thresholds(p: np.ndarray, labels: np.ndarray) -> list[float]:
    out = []
    for j in range(3):
        best_t, best_f1 = 0.5, -1.0
        for t in np.arange(0.05, 0.96, 0.01):
            a = p[:, j] >= t
            tp = (a & labels[:, j]).sum()
            f1 = 2 * tp / max(a.sum() + labels[:, j].sum(), 1)
            if f1 > best_f1:
                best_t, best_f1 = float(t), f1
        out.append(round(best_t, 2))
    return out


def ood_stats(features: np.ndarray, embeddings: np.ndarray) -> dict[str, Any]:
    mean, std = embeddings.mean(axis=0), embeddings.std(axis=0) + 1e-6
    dist = np.sqrt((((embeddings - mean) / std) ** 2).mean(axis=1))
    return {
        "feature_order": gf.NODE_FEATURE_ORDER,
        "feature_min": features.min(axis=0).round(4).tolist(), "feature_max": features.max(axis=0).round(4).tolist(),
        "feature_p01": np.percentile(features, 1, axis=0).round(4).tolist(),
        "feature_p99": np.percentile(features, 99, axis=0).round(4).tolist(),
        "embedding_mean": mean.round(5).tolist(), "embedding_std": std.round(5).tolist(),
        "embedding_distance_p95": round(float(np.percentile(dist, 95)), 4),
        "embedding_distance_p99": round(float(np.percentile(dist, 99)), 4),
        "definition": "embedding distance = RMS of per-dimension z-scores against train-map embeddings "
                      "(cascade-relevant entities); features in NODE_FEATURE_ORDER",
    }


# --- evaluation --------------------------------------------------------------------------------
def evaluate_split(split: Split, preds: list[dict], temps: list[float], thresholds: list[float],
                   v2_threshold: float, bootstrap: int, seed: int) -> dict[str, Any]:
    runs, i = [], 0
    single_map = len({r["map_id"] for r in split.runs}) == 1
    for r in split.runs:
        npz = r["npz"]
        S = len(npz["snapshot_step"])
        block = preds[i:i + S]
        i += S
        p = np.stack([calibrated(b["logits"], temps) for b in block])
        v3 = {"scores": p, "alerts": p >= np.array(thresholds)[None, None, :],
              "ttc": np.stack([b["ttc"] for b in block])}
        run = ev.Run(cluster=r["file"] if single_map else r["map_id"], mask=r["mask"], labels=npz["labels"],
                     ttc_true=npz["ttc_true"], snapshot_step=npz["snapshot_step"], crossings=r["crossings"],
                     cycle_sec=int(npz["cycle_sec"]),
                     predictions={"hx_cascade_v3": v3, **ev.baseline_predictions(npz, v2_threshold)})
        runs.append(run)
    names = sorted(set.intersection(*[set(r.predictions) for r in runs])) if runs else []
    results = {name: ev.evaluate(runs, name, probabilistic=name.startswith("hx_cascade"), bootstrap=bootstrap,
                                 seed=seed) for name in names}
    return {"runs": len(runs), "maps": len({r["map_id"] for r in split.runs}), "results": results}


def _git_commit() -> str | None:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True,
                              check=True).stdout.strip()
    except Exception:
        return None


def _library_versions() -> dict[str, str]:
    import platform

    import torch_geometric

    return {"python": platform.python_version(), "torch": torch.__version__,
            "torch_geometric": torch_geometric.__version__, "numpy": np.__version__}


def _configs(args: argparse.Namespace) -> list[dict[str, Any]]:
    """The base configuration from the flags, or one per `--sweep` entry (each
    entry overrides some of the base keys)."""
    base = {"hidden": args.hidden, "layers": args.layers, "heads": args.heads, "dropout": args.dropout,
            "lr": args.lr, "batch_size": args.batch_size, "pos_weight": args.pos_weight}
    if not args.sweep:
        return [base]
    raw = Path(args.sweep).read_text(encoding="utf-8") if Path(args.sweep).is_file() else args.sweep
    entries = json.loads(raw)
    unknown = {k for e in entries for k in e} - set(base)
    if unknown:
        raise SystemExit(f"unknown sweep keys {sorted(unknown)}; allowed {sorted(base)}")
    return [{**base, **e} for e in entries]


def main() -> int:
    ap = argparse.ArgumentParser(description="Train, calibrate and evaluate HX-Cascade v3.")
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True, help="bundle directory to create")
    ap.add_argument("--version", default=None, help="model_version (default: the --out directory name)")
    ap.add_argument("--dataset-version", default=None, help="e.g. the Kaggle dataset/notebook version it came from")
    ap.add_argument("--source-commit", default=None, help="code commit (default: git rev-parse HEAD)")
    ap.add_argument("--epochs", type=int, default=40)
    ap.add_argument("--patience", type=int, default=6)
    ap.add_argument("--batch-size", type=int, default=32)
    ap.add_argument("--lr", type=float, default=2e-3)
    ap.add_argument("--hidden", type=int, default=64)
    ap.add_argument("--layers", type=int, default=3)
    ap.add_argument("--heads", type=int, default=4)
    ap.add_argument("--dropout", type=float, default=0.1)
    ap.add_argument("--pos-weight", default="sqrt", choices=POS_WEIGHT_MODES, help="class weighting of positives")
    ap.add_argument("--sweep", default=None,
                    help="JSON list (or a file holding one) of configs overriding the flags above, e.g. "
                         "[{\"hidden\": 128, \"lr\": 0.001}]; the winner is chosen on validation maps only "
                         "(mean AP over horizons)")
    ap.add_argument("--ttc-weight", type=float, default=0.5)
    ap.add_argument("--v2-threshold", type=float, default=0.6, help="v2's deployed gnn_min_probability")
    ap.add_argument("--bootstrap", type=int, default=200)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--device", default="auto", help="auto | cpu | cuda (training only; evaluation runs on CPU)")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    if args.out.exists() and not args.force:
        raise SystemExit(f"{args.out} exists; pass --force to overwrite")
    configs = _configs(args)
    started = time.perf_counter()

    dataset = json.loads((args.data / "dataset.json").read_text(encoding="utf-8"))
    scored = tuple(dataset["scored_types"])
    splits = {s: Split(args.data, s, scored) for s in ("train", "val", "test", "live")}
    graphs = {s: splits[s].graphs() for s in splits}
    maps = {s: len({r["map_id"] for r in splits[s].runs}) for s in splits}
    print({s: len(g) for s, g in graphs.items()}, "snapshot graphs;", maps, "maps", flush=True)
    if not graphs["train"] or not graphs["val"]:
        raise SystemExit("the dataset has no train or no validation runs")

    timing: dict[str, Any] = {"load_sec": round(time.perf_counter() - started, 1)}
    device = torch.device("cuda" if args.device == "auto" and torch.cuda.is_available()
                          else ("cpu" if args.device == "auto" else args.device))
    print("training on", device, flush=True)

    # 1. sweep: every config trained on train maps, scored on validation maps only.
    t0 = time.perf_counter()
    sweep, best = [], None
    for i, cfg in enumerate(configs):
        print(f"config {i + 1}/{len(configs)}: {cfg}", flush=True)
        c0 = time.perf_counter()
        model, training = train(graphs["train"], graphs["val"], cfg, args, device)
        score = selection_score(_predict(model, graphs["val"], torch.device("cpu")), graphs["val"])
        sweep.append({"config": cfg, "val_mean_ap": round(score, 5),
                      "best_val_loss": round(training["best_val_loss"], 5),
                      "epochs": len(training["history"]), "seconds": round(time.perf_counter() - c0, 1)})
        print(f"  -> validation mean AP {score:.4f}", flush=True)
        if best is None or score > best[0]:
            best = (score, cfg, model, training)
    _, cfg, model, training = best
    training.update(device=str(device), config=cfg, sweep=sweep,
                    selection="max mean average precision over 900/1800/3600 s on validation maps")
    timing["train_sec"] = round(time.perf_counter() - t0, 1)
    print("selected", cfg, flush=True)

    # 2. calibrate and choose thresholds on validation maps; OOD statistics on train maps.
    preds = {s: _predict(model, graphs[s], torch.device("cpu")) for s in splits}
    val_mask = np.concatenate([g.mask.numpy() for g in graphs["val"]])
    val_logits = np.concatenate([p["logits"] for p in preds["val"]])[val_mask]
    val_labels = np.concatenate([g.y.numpy() for g in graphs["val"]])[val_mask] > 0.5
    temps = fit_temperatures(val_logits, val_labels)
    thresholds = choose_thresholds(calibrated(val_logits, temps), val_labels)
    print("temperatures", temps, "thresholds", thresholds, flush=True)

    train_mask = np.concatenate([g.mask.numpy() for g in graphs["train"]])
    ood = ood_stats(np.concatenate([g.x.numpy() for g in graphs["train"]])[train_mask],
                    np.concatenate([p["embedding"] for p in preds["train"]])[train_mask])

    # 3. evaluate the one selected, calibrated model; nothing is tuned on test or live.
    report: dict[str, Any] = {
        "model": "hx_cascade_v3", "horizons_sec": list(ev.HORIZONS), "scored_types": list(scored),
        "sample_definition": "scored entity types not over their critical line in the published state",
        "operating_points": {"hx_cascade_v3": {"thresholds": dict(zip(map(str, ev.HORIZONS), thresholds))},
                             "hx_cascade_v2": {"threshold": args.v2_threshold},
                             "deterministic_cascade": "a published cascade step projected critical with eta <= h",
                             "forecast_rule": "the forecaster's time_to_critical_sec <= h"},
        "model_selection": "validation maps only; test maps and the live map are evaluated once, after selection "
                           "and calibration",
        "validity": "in simulation only; maps are random variations of one synthetic city (03 §8.4)",
        "caveats": {"hx_cascade_v2": "v2 was trained on the live map, so its `live` numbers are in-sample; "
                                     "only its val/test numbers are held out"},
    }
    t0 = time.perf_counter()
    for s in ("val", "test", "live"):
        if splits[s].runs:
            report[s] = evaluate_split(splits[s], preds[s], temps, thresholds, args.v2_threshold,
                                       args.bootstrap, args.seed)
    if "test" in report and "hx_cascade_v3" in report["test"]["results"]:
        report["swap_criterion_test"] = ev.swap_criterion(report["test"]["results"], "hx_cascade_v3")
    timing["evaluate_sec"] = round(time.perf_counter() - t0, 1)
    timing["graphs"] = {s: len(g) for s, g in graphs.items()}

    # 4. the bundle.
    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    torch.save(model.state_dict(), out / "model.pt")
    norm = {"builder": "ML/features/graph_features.py", "node_feature_order": gf.NODE_FEATURE_ORDER,
            "edge_feature_order": gf.EDGE_FEATURE_ORDER, "node_feat_dim": gf.NODE_FEAT_DIM,
            "edge_feat_dim": gf.EDGE_FEAT_DIM, "cascade_relevant_types": list(scored),
            "constants": {"util_clip": gf.UTIL_CLIP, "cap_ref": gf.CAP_REF, "degree_div": gf.DEGREE_DIV,
                          "travel_div": gf.TRAVEL_DIV, "flow_scale": gf.FLOW_SCALE},
            "label_thresholds": dataset["thresholds"]}
    (out / "feature_norm.json").write_text(json.dumps(norm, indent=2), encoding="utf-8")
    (out / "calibration.json").write_text(json.dumps({
        "temperature": dict(zip(map(str, ev.HORIZONS), temps)),
        "alert_threshold": dict(zip(map(str, ev.HORIZONS), thresholds)),
        # A temperature on the search bound was not fitted, only clipped: treat
        # that horizon's probabilities as uncalibrated.
        "at_search_bound": {str(h): t in TEMPERATURE_RANGE for h, t in zip(ev.HORIZONS, temps)},
        "fitted_on": "validation maps"}, indent=2), encoding="utf-8")
    (out / "ood_stats.json").write_text(json.dumps(ood, indent=1), encoding="utf-8")
    (out / "eval.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (out / "training.json").write_text(json.dumps({**training, "args": {k: str(v) for k, v in vars(args).items()},
                                                   "dataset": dataset, "timing": timing,
                                                   "seconds": round(time.perf_counter() - started, 1)},
                                                  indent=2), encoding="utf-8")
    write_manifest(
        out, model_name="hx_cascade", model_version=args.version or out.name,
        architecture={"class": "HXCascadeV3", "node_feat_dim": gf.NODE_FEAT_DIM, "edge_feat_dim": gf.EDGE_FEAT_DIM,
                      "hidden": cfg["hidden"], "num_layers": cfg["layers"], "heads": cfg["heads"],
                      "dropout": cfg["dropout"]},
        topology_hash=None, calibrated=True,
        evaluated_outputs=["failure_900", "failure_1800", "failure_3600", "ttc"],
        trained_on=(f"{maps['train']} random maps (train), {maps['val']} (validation: model selection, early "
                    f"stopping, calibration, thresholds); {maps['test']} test maps and the live map held out"),
        label_thresholds=dataset["thresholds"],
        source_commit=args.source_commit or _git_commit(),
        dataset={"version": args.dataset_version, "source_commit": dataset.get("source_commit"),
                 "seed": (dataset.get("args") or {}).get("seed"), "runs": dataset.get("runs"), "maps": maps},
        seeds={"train": args.seed, "dataset": (dataset.get("args") or {}).get("seed")},
        library_versions=_library_versions(),
        created_by="ML/training/train_v3.py",
    )
    print(f"bundle written to {out} in {time.perf_counter() - started:.0f}s", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
