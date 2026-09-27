"""The one feature builder for HX-Cascade v3 — imported by training AND serving.

Training (`ML/training/train_v3.py`) calls `node_features` / `edge_features` on
the arrays the dataset generator recorded; serving (`ML/cascade.py`) first turns
`node_state_for_ml()` into the same arrays with `arrays_from_node_state` and then
calls the same two functions. There is no second copy to drift.

Every feature is scale-free so the model can transfer across maps: capacities
are log-scaled against a fixed reference, loads are also expressed as headroom
to the entity's own critical line, degrees and travel times use fixed divisors
(no per-map maxima). NumPy only; the model code turns the arrays into tensors.
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np

ENTITY_TYPES = ["venue", "zone", "transport_node", "transport_route", "road",
                "hotel", "parking", "gate", "emergency_facility"]
EDGE_TYPES = ["feeds", "adjacent_to", "serves", "last_mile_to", "substitutes_for", "evacuates_to"]
PEOPLE_EDGES = ("feeds", "adjacent_to", "serves", "last_mile_to")
CASCADE_RELEVANT_TYPES = ["gate", "road", "transport_node", "emergency_facility"]
HORIZONS = (900, 1800, 3600)

UTIL_CLIP = 3.0
CAP_REF = 1e5            # log1p(capacity) / log1p(CAP_REF)
DEGREE_DIV = 8.0
TRAVEL_DIV = 1800.0
FLOW_SCALE = 15.0        # utilisation change per 15 minutes

NODE_FEATURE_ORDER = (
    ["util", "forecast_900", "forecast_1800", "forecast_3600", "critical_line",
     "headroom_now", "headroom_900", "headroom_1800", "headroom_3600",
     "log_capacity", "is_observed", "flow_rel", "in_degree", "out_degree"]
    + [f"type_{t}" for t in ENTITY_TYPES]
)
EDGE_FEATURE_ORDER = [f"edge_{t}" for t in EDGE_TYPES] + [
    "transfer_coefficient", "travel_time", "substitutability", "is_reverse"]
NODE_FEAT_DIM = len(NODE_FEATURE_ORDER)
EDGE_FEAT_DIM = len(EDGE_FEATURE_ORDER)


def critical_lines(types: list[str], thresholds: dict[str, Any]) -> np.ndarray:
    """Per-entity critical utilisation from the config `thresholds` block
    (`critical_utilisation`, `by_type.<type>.critical`) — the same lines the
    labels and the live risk bands use."""
    default = float(thresholds.get("critical_utilisation", 0.90))
    by_type = thresholds.get("by_type") or {}
    return np.array([float((by_type.get(t) or {}).get("critical", default)) for t in types], dtype=np.float32)


def static_graph(entity_ids: list[str], types: list[str], edges: list[dict]) -> dict[str, np.ndarray]:
    """Per-map structure: in/out degree over people edges, and the directed edge
    list in both directions (reverse copies flagged), with their attributes."""
    idx = {e: i for i, e in enumerate(entity_ids)}
    n = len(entity_ids)
    in_deg = np.zeros(n, dtype=np.float32)
    out_deg = np.zeros(n, dtype=np.float32)
    src, dst, attr = [], [], []
    for e in edges:
        s, d, t = e["src_entity_id"], e["dst_entity_id"], e["edge_type"]
        if s not in idx or d not in idx or t not in EDGE_TYPES:
            continue
        if t in PEOPLE_EDGES:
            out_deg[idx[s]] += 1
            in_deg[idx[d]] += 1
        base = [0.0] * len(EDGE_TYPES)
        base[EDGE_TYPES.index(t)] = 1.0
        values = [float(e.get("transfer_coefficient") or 0.0),
                  min(float(e.get("travel_time_sec") or 0.0) / TRAVEL_DIV, 3.0),
                  float(e.get("substitutability") or 0.0)]
        for reverse in (False, True):
            src.append(idx[d] if reverse else idx[s])
            dst.append(idx[s] if reverse else idx[d])
            attr.append(base + values + [1.0 if reverse else 0.0])
    return {
        "edge_index": np.array([src, dst], dtype=np.int64).reshape(2, -1),
        "edge_attr": np.array(attr, dtype=np.float32).reshape(-1, EDGE_FEAT_DIM),
        "in_degree": in_deg, "out_degree": out_deg,
    }


def node_features(util: np.ndarray, forecasts: np.ndarray, capacity: np.ndarray, types: list[str],
                  crit: np.ndarray, is_observed: np.ndarray, flow_rate_per_min: np.ndarray,
                  graph: dict[str, np.ndarray]) -> np.ndarray:
    """(N, NODE_FEAT_DIM). `forecasts` is (N, 3) for 900/1800/3600 s; NaN means
    no forecast yet and is replaced by the current utilisation."""
    util = np.clip(np.nan_to_num(util.astype(np.float32)), 0.0, UTIL_CLIP)
    f = forecasts.astype(np.float32).copy()
    missing = np.isnan(f)
    f[missing] = np.broadcast_to(util[:, None], f.shape)[missing]
    f = np.clip(f, 0.0, UTIL_CLIP)
    cap = np.maximum(capacity.astype(np.float32), 1.0)
    x = np.zeros((len(util), NODE_FEAT_DIM), dtype=np.float32)
    x[:, 0] = util
    x[:, 1:4] = f
    x[:, 4] = crit
    x[:, 5] = crit - util
    x[:, 6:9] = crit[:, None] - f
    x[:, 9] = np.log1p(cap) / math.log1p(CAP_REF)
    x[:, 10] = is_observed.astype(np.float32)
    x[:, 11] = np.clip(np.nan_to_num(flow_rate_per_min.astype(np.float32)) / cap * FLOW_SCALE, -3.0, 3.0)
    x[:, 12] = graph["in_degree"] / DEGREE_DIV
    x[:, 13] = graph["out_degree"] / DEGREE_DIV
    for i, t in enumerate(types):
        if t in ENTITY_TYPES:
            x[i, 14 + ENTITY_TYPES.index(t)] = 1.0
    return x


def arrays_from_node_state(node_state: dict[str, dict]) -> dict[str, Any]:
    """Serving path: `node_state_for_ml()` -> the arrays `node_features` takes."""
    ids = list(node_state)
    get = lambda k, default=np.nan: np.array(  # noqa: E731
        [default if node_state[e].get(k) is None else float(node_state[e][k]) for e in ids], dtype=np.float32)
    return {
        "entity_ids": ids,
        "types": [node_state[e].get("entity_type", "") for e in ids],
        "util": get("utilisation", 0.0),
        "forecasts": np.stack([get(f"forecast_{h}") for h in HORIZONS], axis=1),
        "capacity": get("nominal_capacity", 1.0),
        "is_observed": np.array([bool(node_state[e].get("is_observed", False)) for e in ids], dtype=np.float32),
        "flow_rate_per_min": get("flow_rate_per_min", 0.0),
    }


def build(node_state: dict[str, dict], edges: list[dict], thresholds: dict[str, Any]) -> dict[str, Any]:
    """Serving convenience: everything the v3 model needs for one graph."""
    a = arrays_from_node_state(node_state)
    graph = static_graph(a["entity_ids"], a["types"], edges)
    crit = critical_lines(a["types"], thresholds)
    x = node_features(a["util"], a["forecasts"], a["capacity"], a["types"], crit, a["is_observed"],
                      a["flow_rate_per_min"], graph)
    return {"entity_ids": a["entity_ids"], "types": a["types"], "x": x, "critical": crit, **graph}
