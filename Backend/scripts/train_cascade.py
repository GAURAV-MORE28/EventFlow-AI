"""Retrain the HX-Cascade R-GCN on the flow-coupled city simulator.

    python -m scripts.train_cascade --scenarios 80 --epochs 40 --name hx_cascade_v2b

The v1 checkpoint was trained on data from the old per-entity curve generator,
which has no flow between entities. This script generates training data from
the current simulator (the same physics the live product runs), trains the same
2-layer R-GCN architecture (`ML/models/hx_cascade.py::HXCascade`) with forecast features
added, and evaluates it on held-out scenarios against a transparent rule
baseline ("the node's own forecast crosses its critical line").

Output: a verified bundle ML/artifacts/<name>/ (see ML/manifest.py)
  model.pt             weights
  feature_norm.json    feature layout the inference code reads
  eval.json            held-out metrics for the model AND the baseline
  manifest.json        sha256 of each file, topology hash, provenance

The bundle is only switched on (config.yaml `cascade.gnn_artifact`) if the
held-out results justify it; the evaluation file records the numbers either way.
Scenarios are seeded, so the run is reproducible. Held-out data here are
held-out *scenarios on the training map*, not held-out topologies.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.catalog import build_properties  # noqa: E402
from app.config import get_config  # noqa: E402
from app.ml_reference.generator import SyntheticGenerator  # noqa: E402
from app.topology import build_topology  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from ML.models.hx_cascade import HXCascade  # noqa: E402

HORIZONS = [900, 1800, 3600]
TYPES = ["venue", "zone", "transport_node", "transport_route", "road", "hotel", "parking", "gate", "emergency_facility"]
EDGE_TYPES = ["feeds", "adjacent_to", "serves", "last_mile_to", "substitutes_for", "evacuates_to"]
RELEVANT = ["emergency_facility", "gate", "road", "transport_node"]
STEP = 30
DECAY = 1800.0


def make_generator(seed: int, events: list[dict]) -> SyntheticGenerator:
    cfg = get_config().raw
    topo = build_topology()
    topo["properties"] = build_properties(topo["nodes"], topo["edges"])
    topo["events"] = events
    return SyntheticGenerator(
        {"demand": cfg["demand"], "hospitality": cfg["hospitality"],
         "sim_start_time": cfg["event"]["sim_start_time"], "critical_utilisation": 0.9},
        topo, seed,
    )


def random_disruptions(rng: np.random.Generator, gen: SyntheticGenerator) -> list[tuple[int, str, dict]]:
    """A seeded mix of the disruptions the product supports, at random times."""
    gates = [e for e, t in gen.types.items() if t == "gate"]
    stations = [e for e, t in gen.types.items() if t == "transport_node"]
    roads = [e for e, t in gen.types.items() if t == "road"]
    out = []
    for _ in range(int(rng.integers(0, 4))):
        step = int(rng.integers(40, 500))
        kind = rng.choice(["gate_closure", "transport_outage", "attendance_delta", "weather_rain",
                           "road_capacity_delta", "metro_capacity_delta", "event_delay"])
        if kind == "gate_closure":
            out.append((step, kind, {"entity_id": str(rng.choice(gates))}))
        elif kind == "transport_outage":
            out.append((step, kind, {"entity_id": str(rng.choice(stations))}))
        elif kind == "attendance_delta":
            out.append((step, kind, {"delta_pct": float(rng.uniform(-20, 35))}))
        elif kind == "weather_rain":
            out.append((step, kind, {"intensity": str(rng.choice(["moderate", "heavy"]))}))
        elif kind == "road_capacity_delta":
            out.append((step, kind, {"entity_id": str(rng.choice(roads)), "delta_pct": float(rng.uniform(-60, -20))}))
        elif kind == "metro_capacity_delta":
            out.append((step, kind, {"entity_id": str(rng.choice(stations)), "delta_pct": float(rng.uniform(-50, -15))}))
        else:
            out.append((step, kind, {"event_id": "evt_demo", "delay_min": float(rng.uniform(10, 45))}))
    return sorted(out)


def run_scenario(seed: int, events: list[dict], steps: int = 700, snap_every: int = 20):
    rng = np.random.default_rng(seed)
    scen_events = [dict(e) for e in events]
    gen = make_generator(seed, scen_events)
    nominal = gen.clone(sources={"intervention", "schedule"})
    plan = random_disruptions(rng, gen)
    crit = {e: (1.02 if t == "venue" else 0.98 if t == "hotel" else 0.90) for e, t in gen.types.items()}
    ids = list(gen.types)
    traj = []
    snaps = []
    for k in range(steps):
        while plan and plan[0][0] == k:
            _, kind, params = plan.pop(0)
            gen.inject(kind, params)
        gen.tick(STEP)
        nominal.tick(STEP)
        u = gen.utilisation()
        traj.append([u[e] for e in ids])
        if k >= 30 and k % snap_every == 0 and k + 120 < steps:
            # Forecast features exactly as the live forecaster builds them:
            # nominal projection + decaying gap to the current reading.
            proj = nominal.clone()
            now_nom = proj.utilisation()
            pts = {}
            elapsed = 0
            for h in HORIZONS:
                while elapsed < h:
                    proj.tick(60)
                    elapsed += 60
                pts[h] = proj.utilisation()
            f = {h: [max(0.0, pts[h][e] + (u[e] - now_nom[e]) * math.exp(-h / DECAY)) for e in ids] for h in HORIZONS}
            snaps.append((k, [u[e] for e in ids], f))
    traj = np.array(traj)
    samples = []
    for k, util, f in snaps:
        labels, ttc = [], []
        for i, e in enumerate(ids):
            future = traj[k + 1: k + 1 + 3600 // STEP, i]
            over = np.nonzero(future >= crit[e])[0]
            first = (over[0] + 1) * STEP if len(over) else None
            labels.append([1.0 if first is not None and first <= h else 0.0 for h in HORIZONS])
            ttc.append(float(first) if first is not None else 3600.0)
        samples.append({"util": util, "f": f, "y": labels, "ttc": ttc})
    return ids, samples, crit


def build_static(gen_ids, types, edges, norm_cap):
    idx = {e: i for i, e in enumerate(gen_ids)}
    src, dst, et = [], [], []
    for e in edges:
        if e["src_entity_id"] in idx and e["dst_entity_id"] in idx:
            src.append(idx[e["src_entity_id"]])
            dst.append(idx[e["dst_entity_id"]])
            et.append(EDGE_TYPES.index(e["edge_type"]))
    indeg = np.zeros(len(gen_ids))
    for e in edges:
        if e["edge_type"] in ("feeds", "last_mile_to", "serves", "evacuates_to") and e["dst_entity_id"] in idx:
            indeg[idx[e["dst_entity_id"]]] += 1
    return src, dst, et, indeg


def features(sample, gen_ids, types, caps, cap_const, indeg, max_deg):
    x = np.zeros((len(gen_ids), 2 + len(TYPES) + 1 + 3), dtype=np.float32)
    for i, e in enumerate(gen_ids):
        x[i, 0] = min(sample["util"][i], 3.0)
        x[i, 1] = caps[e] / cap_const
        x[i, 2 + TYPES.index(types[e])] = 1.0
        x[i, 2 + len(TYPES)] = indeg[i] / max_deg
        for j, h in enumerate(HORIZONS):
            x[i, 3 + len(TYPES) + j] = min(sample["f"][h][i], 3.0)
    return x


def average_precision(scores: np.ndarray, labels: np.ndarray) -> float:
    order = np.argsort(-scores)
    y = labels[order]
    if y.sum() == 0:
        return float("nan")
    tp = np.cumsum(y)
    precision = tp / (np.arange(len(y)) + 1)
    return float((precision * y).sum() / y.sum())


def prf(pred: np.ndarray, labels: np.ndarray) -> dict:
    tp = float((pred & (labels == 1)).sum())
    fp = float((pred & (labels == 0)).sum())
    fn = float((~pred & (labels == 1)).sum())
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    return {"precision": round(p, 3), "recall": round(r, 3), "f1": round(2 * p * r / (p + r), 3) if p + r else 0.0}


def _git_commit() -> str | None:
    import subprocess

    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True,
                              check=True).stdout.strip()
    except Exception:
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenarios", type=int, default=80)
    ap.add_argument("--heldout", type=int, default=20)
    ap.add_argument("--epochs", type=int, default=40)
    ap.add_argument("--name", default="hx_cascade_v2b", help="bundle directory under ML/artifacts/")
    ap.add_argument("--force", action="store_true", help="overwrite an existing bundle")
    args = ap.parse_args()
    out_dir = REPO / "ML" / "artifacts" / args.name
    if out_dir.exists() and not args.force:
        raise SystemExit(f"{out_dir} exists; pick another --name or pass --force")

    import torch
    import torch.nn.functional as F

    torch.manual_seed(42)
    cfg = get_config().raw
    events = cfg["events"]
    topo = build_topology()
    types = {n["entity_id"]: n["entity_type"] for n in topo["nodes"]}
    caps = {n["entity_id"]: float(n["nominal_capacity"]) for n in topo["nodes"]}
    cap_const = max(caps.values())

    t0 = time.time()
    data, gen_ids, crit = [], None, None
    for s in range(args.scenarios + args.heldout):
        ids, samples, crit = run_scenario(1000 + s, events)
        gen_ids = ids
        data.append(samples)
    print(f"generated {sum(len(d) for d in data)} snapshots from {len(data)} scenarios in {time.time() - t0:.0f}s")

    src, dst, et, indeg = build_static(gen_ids, types, topo["edges"], cap_const)
    max_deg = max(float(indeg.max()), 1.0)
    edge_index = torch.tensor([src, dst], dtype=torch.long)
    edge_type = torch.tensor(et, dtype=torch.long)
    relevant = torch.tensor([types[e] in RELEVANT for e in gen_ids])

    def tensors(samples):
        X = [torch.tensor(features(s, gen_ids, types, caps, cap_const, indeg, max_deg)) for s in samples]
        Y = [torch.tensor(s["y"], dtype=torch.float32) for s in samples]
        T = [torch.tensor(s["ttc"], dtype=torch.float32) for s in samples]
        return X, Y, T

    train = [s for d in data[: args.scenarios] for s in d]
    test = [s for d in data[args.scenarios:] for s in d]
    Xtr, Ytr, Ttr = tensors(train)
    Xte, Yte, Tte = tensors(test)

    model = HXCascade(node_feat_dim=Xtr[0].shape[1])
    opt = torch.optim.Adam(model.parameters(), lr=3e-3, weight_decay=1e-5)
    pos = torch.stack(Ytr)[:, relevant].mean(dim=(0, 1)).clamp(min=1e-3)
    pos_weight = ((1 - pos) / pos).clamp(max=20.0)
    for epoch in range(args.epochs):
        model.train()
        perm = torch.randperm(len(Xtr))
        total = 0.0
        for i in perm.tolist():
            out = model(Xtr[i], edge_index, edge_type)
            loss = F.binary_cross_entropy_with_logits(out["failure_logits"][relevant], Ytr[i][relevant], pos_weight=pos_weight)
            hit = Ytr[i][:, 2] > 0
            if hit.any():
                loss = loss + 0.2 * F.l1_loss(out["ttc"][hit], Ttr[i][hit]) / 3600.0
            opt.zero_grad()
            loss.backward()
            opt.step()
            total += float(loss)
        if epoch % 10 == 9:
            print(f"epoch {epoch + 1}: loss {total / len(Xtr):.4f}")

    # --- held-out evaluation: model vs forecast-threshold baseline ---------------------
    model.eval()
    crit_vec = torch.tensor([crit[e] for e in gen_ids])
    report = {"horizons_sec": HORIZONS, "train_scenarios": args.scenarios, "heldout_scenarios": args.heldout,
              "train_snapshots": len(Xtr), "heldout_snapshots": len(Xte),
              "evaluated_on": "cascade-relevant nodes (gate, road, transport_node, emergency_facility)",
              "model": {}, "baseline_forecast_threshold": {}}
    with torch.no_grad():
        probs = torch.stack([torch.sigmoid(model(x, edge_index, edge_type)["failure_logits"]) for x in Xte])
    Y = torch.stack(Yte)
    Xs = torch.stack(Xte)
    for j, h in enumerate(HORIZONS):
        p = probs[:, relevant, j].reshape(-1).numpy()
        y = Y[:, relevant, j].reshape(-1).numpy()
        f = Xs[:, relevant, 3 + len(TYPES) + j].reshape(-1).numpy()
        c = crit_vec[relevant].repeat(len(Xte)).numpy()
        # Baseline score: how far the node's own forecast sits above its critical line.
        base_score = f - c
        report["model"][str(h)] = {"average_precision": round(average_precision(p, y), 3),
                                   **prf(p >= 0.6, y.astype(int))}
        report["baseline_forecast_threshold"][str(h)] = {"average_precision": round(average_precision(base_score, y), 3),
                                                         **prf(base_score >= 0, y.astype(int))}
        report["model"][str(h)]["positives"] = int(y.sum())
    print(json.dumps(report, indent=1))

    from ML.manifest import topology_hash, write_manifest

    out_dir.mkdir(parents=True, exist_ok=True)
    torch.save(model.state_dict(), out_dir / "model.pt")
    norm = {
        "capacity_norm_const": cap_const, "max_degree_seen_in_training": max_deg,
        "entity_type_order": TYPES, "edge_type_order": EDGE_TYPES, "util_clip": 3.0,
        "node_feature_order": (["util_at_injection_clipped", "capacity_norm"] + [f"entity_type_{t}" for t in TYPES]
                               + ["degree_norm", "forecast_900", "forecast_1800", "forecast_3600"]),
        "node_feat_dim": int(Xtr[0].shape[1]), "cascade_relevant_types": RELEVANT,
        "pos_weight_by_horizon": {str(h): round(float(w), 2) for h, w in zip(HORIZONS, pos_weight.tolist())},
        "forecast_features": True, "training_source": "flow_simulator_v2",
    }
    (out_dir / "feature_norm.json").write_text(json.dumps(norm, indent=2))
    wins = sum(report["model"][str(h)]["average_precision"] > report["baseline_forecast_threshold"][str(h)]["average_precision"]
               for h in HORIZONS)
    report["decision"] = "use_v2" if wins >= 2 else "keep_baseline_rule"
    (out_dir / "eval.json").write_text(json.dumps(report, indent=2))
    write_manifest(
        out_dir, model_name="hx_cascade", model_version=args.name,
        architecture={"class": "HXCascade", "node_feat_dim": norm["node_feat_dim"], "hidden": 64,
                      "num_layers": 2, "num_edge_types": len(EDGE_TYPES)},
        topology_hash=topology_hash(topo["nodes"], topo["edges"]), calibrated=False,
        evaluated_outputs=[f"failure_{h}" for h in HORIZONS], unevaluated_outputs=["ttc"],
        trained_on=(f"flow simulator: {args.scenarios} train / {args.heldout} held-out scenarios on ONE topology; "
                    "held-out scenarios, not held-out topologies"),
        created_by="Backend/scripts/train_cascade.py", source_commit=_git_commit(),
    )
    print("bundle:", out_dir)
    print("decision:", report["decision"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
