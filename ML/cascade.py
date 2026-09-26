"""CascadePredictor — HX-Cascade GNN (03_ML_CONTRACT.md §4).

Drop-in per `ML/README.md`'s contract: `Backend/app/ml_registry.py` imports this
module in place of `app.ml_reference.cascade` the moment `use_gnn: true` is set
in `Backend/config.yaml`, with zero other backend changes.

Swap justification (see `swap_decision_v1.json`): the deterministic propagator
fails the recall>=0.70 bar outright (0.322), so the GNN wins on recall
regardless of held-out-pool small-sample inflation, and beats it 13-43x on
lead-time MAE even on the conservative train-pool figure (AP=0.836,
precision@recall>=0.70=0.797, TTC_MAE~=204s). Never advertise the held-out-only
figures (AP=0.900, precision=0.996) as the trustworthy estimate.

Deviations from the contract's original §4.3 spec, disclosed and deliberate:
  - Single-shot multi-horizon prediction, not autoregressive rollout. The
    checkpoint was trained this way; feeding its own outputs back in as
    features would leave the network operating outside its training
    distribution.
  - Only `cascade_relevant_types` (gate, road, transport_node,
    emergency_facility) have their failure/TTC predictions surfaced. Other
    types still participate in message passing (they carry real state that
    shapes their neighbours' predictions) but are graph context, not trusted
    outputs, per the held-out evaluation only covering the relevant types.

No I/O after `__init__` (03 §0 rule 1): the checkpoint and `feature_norm.json`
load once at construction; `predict`/`fallback` touch only their arguments.
No imports from `Backend/` (03 §0 rule 2): the deterministic fallback below is
a self-contained copy of `app/ml_reference/cascade.py`'s propagator, not an
import of it.
"""
from __future__ import annotations

import json
import logging
from collections import deque
from pathlib import Path
from typing import Any

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch_geometric.nn import RGCNConv

log = logging.getLogger("eventflow.ml.cascade")

_HERE = Path(__file__).resolve().parent
_REPO_ROOT_GUESS = _HERE.parent


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _band_from_score(score: float, bands: dict[str, float]) -> str:
    if score <= bands.get("low", 30):
        return "low"
    if score <= bands.get("moderate", 60):
        return "moderate"
    if score <= bands.get("high", 80):
        return "high"
    return "critical"


def _band_from_utilisation(util: float, bands: dict[str, float]) -> str:
    return _band_from_score(_clamp(util * 100.0, 0.0, 100.0), bands)


FLOW_EDGE_TYPES = {"feeds", "adjacent_to", "serves", "last_mile_to", "evacuates_to"}


def _select_roots(node_state: dict[str, dict], critical: float, limit: int, forecast_util) -> list[str]:
    """The most severe roots only: critical now, or high and forecast to cross
    critical. Ranked by risk score then projected load; capped at `limit`."""
    def projected(st: dict) -> float:
        return max(forecast_util(st), float(st.get("utilisation", 0.0)))

    roots = [
        eid for eid, st in node_state.items()
        if st.get("risk_band") == "critical" or (st.get("risk_band") == "high" and projected(st) >= critical)
    ]
    roots.sort(key=lambda e: (-int(node_state[e].get("risk_score", 0)), -projected(node_state[e]), e))
    return roots[:limit]


class HXCascade(nn.Module):
    """Must match the trained checkpoint's shapes exactly — never modify this
    to "fit" a feature mismatch; fix the feature-building code instead."""

    def __init__(self, node_feat_dim: int = 12, hidden: int = 64, num_layers: int = 2, num_edge_types: int = 6):
        super().__init__()
        self.input_proj = nn.Linear(node_feat_dim, hidden)
        self.convs = nn.ModuleList(
            [RGCNConv(hidden, hidden, num_relations=num_edge_types) for _ in range(num_layers)]
        )
        self.norm = nn.ModuleList([nn.LayerNorm(hidden) for _ in range(num_layers)])
        self.head_failure = nn.Linear(hidden, 3)  # logits for [900s, 1800s, 3600s]
        self.head_ttc = nn.Linear(hidden, 1)

    def forward(self, x: torch.Tensor, edge_index: torch.Tensor, edge_type: torch.Tensor) -> dict[str, torch.Tensor]:
        h = torch.relu(self.input_proj(x))
        for conv, norm in zip(self.convs, self.norm):
            h = torch.relu(norm(conv(h, edge_index, edge_type))) + h
        return {
            "failure_logits": self.head_failure(h),
            "ttc": F.relu(self.head_ttc(h)).squeeze(-1),
        }


class CascadePredictor:
    def __init__(self, config: dict) -> None:
        self.config = config or {}
        self.seed = int(self.config.get("seed", 42))
        self.use_gnn = bool(self.config.get("use_gnn", False))
        self.max_depth = int(self.config.get("max_depth", 4))
        self.propagation_threshold = float(self.config.get("propagation_threshold", 0.15))
        self.critical = float(self.config.get("critical_utilisation", 0.90))
        self.bands = self.config.get("risk_bands", {"low": 30, "moderate": 60, "high": 80})
        # Output hygiene (Backend/config.yaml `cascade`): a cascade an operator
        # cannot read is not a prediction, it is noise.
        self.max_roots = int(self.config.get("max_roots", 5))
        self.max_steps = int(self.config.get("max_steps", 8))
        self.exclude_root_types = set(self.config.get("exclude_root_types", ["hotel"]))
        self.min_probability = float(self.config.get("gnn_min_probability", 0.6))
        self.support_util = float(self.config.get("warning_utilisation", 0.75)) * 0.8

        self._model: HXCascade | None = None
        self._norm: dict[str, Any] | None = None
        self._gnn_ready = False
        if self.use_gnn:
            self._load_gnn()

    def ready(self) -> bool:
        return True

    def active_source(self) -> str:
        return "gnn" if (self.use_gnn and self._gnn_ready) else "deterministic"

    # --- model loading (init only — 03 §0 rule 1) ---------------------------
    def _resolve_ml_dir(self) -> Path:
        checkpoint_cfg = self.config.get("gnn_checkpoint")
        if checkpoint_cfg:
            for candidate in (Path(checkpoint_cfg), _REPO_ROOT_GUESS / checkpoint_cfg):
                if candidate.exists():
                    return candidate.parent
        return _HERE  # cascade.py's own directory — where the checkpoint actually ships

    def _load_gnn(self) -> None:
        try:
            torch.manual_seed(self.seed)
            ml_dir = self._resolve_ml_dir()
            with open(ml_dir / "feature_norm.json", "r", encoding="utf-8") as f:
                norm = json.load(f)

            model = HXCascade(node_feat_dim=norm.get("node_feat_dim", 12))
            state_dict = torch.load(ml_dir / "hx_cascade.pt", map_location="cpu", weights_only=True)
            model.load_state_dict(state_dict)
            model.eval()

            self._model = model
            self._norm = norm
            self._sanity_check_dummy_forward()
            self._gnn_ready = True
            log.info("HX-Cascade GNN loaded from %s", ml_dir)
        except Exception:
            log.exception("HX-Cascade GNN failed to load; running deterministic-only")
            self._model = None
            self._norm = None
            self._gnn_ready = False

    def _sanity_check_dummy_forward(self) -> None:
        """A silent feature-mismatch must fail loudly at boot, not produce
        garbage predictions in the demo (this is exactly how the training-time
        leakage bug first showed up)."""
        assert self._model is not None and self._norm is not None
        feat_dim = self._norm["node_feat_dim"]
        x = torch.zeros((4, feat_dim), dtype=torch.float32)
        edge_index = torch.tensor([[0, 1, 2], [1, 2, 3]], dtype=torch.long)
        edge_type = torch.tensor([0, 0, 0], dtype=torch.long)
        with torch.no_grad():
            out = self._model(x, edge_index, edge_type)
        if torch.isnan(out["failure_logits"]).any() or torch.isnan(out["ttc"]).any():
            raise RuntimeError("HX-Cascade GNN sanity forward pass produced NaN")

    # --- 03 §4.1 -------------------------------------------------------------
    def predict(
        self,
        root_entity_id: str,
        node_state: dict[str, dict],
        edges: list[dict],
        max_depth: int | None = None,
        generated_at: str | None = None,
    ) -> dict:
        if self.use_gnn and self._gnn_ready:
            try:
                return self._predict_gnn(root_entity_id, node_state, edges, max_depth, generated_at)
            except Exception:
                log.exception("HX-Cascade GNN predict failed for %s; falling back", root_entity_id)
        return self.fallback(root_entity_id, node_state, edges, max_depth, generated_at)

    def predict_all(
        self,
        node_state: dict[str, dict],
        edges: list[dict],
        generated_at: str | None = None,
    ) -> list[dict]:
        """Every entity currently in band high|critical, or forecast into one,
        becomes a cascade root — matching the >=900s lead-time target (03 §4.4):
        rooting only on the *current* band would make the cascade reactive.

        Runs the GNN forward pass exactly ONCE for the whole cycle and reuses
        it for every root's step-extraction. A demo cycle can have dozens of
        roots at once (that's the point of the cascade demo); one 5ms forward
        pass per root would blow the cascade latency budget on its own before
        this was fixed to share it.
        """
        roots = _select_roots(
            {e: st for e, st in node_state.items() if st.get("entity_type") not in self.exclude_root_types},
            self.critical, self.max_roots, self._forecast_util,
        )

        if self.use_gnn and self._gnn_ready and roots:
            try:
                shared = self._run_gnn_forward(node_state, edges)
            except Exception:
                log.exception("HX-Cascade GNN shared forward pass failed; falling back this cycle")
                shared = None
            if shared is not None:
                results = []
                for root in roots:
                    try:
                        results.append(
                            self._extract_gnn_result(root, node_state, edges, shared, self.max_depth, generated_at)
                        )
                    except Exception:
                        log.exception("HX-Cascade GNN step extraction failed for %s; falling back", root)
                        results.append(self.fallback(root, node_state, edges, None, generated_at))
                return results

        return [self.predict(r, node_state, edges, generated_at=generated_at) for r in roots]

    def fallback(
        self,
        root_entity_id: str,
        node_state: dict[str, dict],
        edges: list[dict],
        max_depth: int | None = None,
        generated_at: str | None = None,
    ) -> dict:
        """Deterministic flow propagation (03 §4.2). Identical return shape to
        `predict`; called automatically on any GNN exception or timeout."""
        return self._propagate_deterministic(root_entity_id, node_state, edges, max_depth, generated_at)

    # --- shared helpers --------------------------------------------------------
    def _forecast_util(self, state: dict, horizon: int = 1800) -> float:
        key = f"forecast_{horizon}"
        if key in state and state[key] is not None:
            return float(state[key])
        return float(state.get("utilisation", 0.0))

    def _empty_result(self, root_entity_id: str, source: str, generated_at: str | None) -> dict:
        return {
            "root_entity_id": root_entity_id,
            "source": source,
            "generated_at": generated_at,
            "total_downstream_failures": 0,
            "max_depth": 0,
            "steps": [],
        }

    # --- GNN path --------------------------------------------------------------
    def _build_graph_tensors(self, node_state: dict[str, dict], edges: list[dict]):
        norm = self._norm
        assert norm is not None
        ids = list(node_state.keys())
        idx = {eid: i for i, eid in enumerate(ids)}

        util_clip = float(norm.get("util_clip", 3.0))
        cap_const = float(norm["capacity_norm_const"])
        type_order: list[str] = norm["entity_type_order"]
        n_types = len(type_order)
        max_degree = max(float(norm.get("max_degree_seen_in_training", 1)), 1.0)
        degree_edge_types = {"feeds", "last_mile_to", "serves", "evacuates_to"}

        in_degree = {eid: 0 for eid in ids}
        for e in edges:
            if e.get("edge_type") in degree_edge_types:
                dst = e.get("dst_entity_id")
                if dst in in_degree:
                    in_degree[dst] += 1

        feat_dim = int(norm.get("node_feat_dim", 12))
        x = torch.zeros((len(ids), feat_dim), dtype=torch.float32)
        for eid, i in idx.items():
            st = node_state[eid]
            util = min(float(st.get("utilisation", 0.0)), util_clip)
            cap = float(st.get("nominal_capacity", 1.0) or 1.0)
            x[i, 0] = util
            x[i, 1] = cap / cap_const
            etype = st.get("entity_type")
            if etype in type_order:
                x[i, 2 + type_order.index(etype)] = 1.0
            x[i, 2 + n_types] = in_degree.get(eid, 0) / max_degree

        edge_type_order: list[str] = norm["edge_type_order"]
        src_list: list[int] = []
        dst_list: list[int] = []
        type_list: list[int] = []
        for e in edges:
            s, d = e.get("src_entity_id"), e.get("dst_entity_id")
            et = e.get("edge_type")
            if s not in idx or d not in idx or et not in edge_type_order:
                continue
            src_list.append(idx[s])
            dst_list.append(idx[d])
            type_list.append(edge_type_order.index(et))

        if src_list:
            edge_index = torch.tensor([src_list, dst_list], dtype=torch.long)
            edge_type = torch.tensor(type_list, dtype=torch.long)
        else:
            edge_index = torch.zeros((2, 0), dtype=torch.long)
            edge_type = torch.zeros((0,), dtype=torch.long)

        return ids, idx, x, edge_index, edge_type

    def _run_gnn_forward(self, node_state: dict[str, dict], edges: list[dict]) -> tuple[dict[str, int], list[float], list[float]]:
        """One forward pass for the whole live graph. Returns (idx, probs, ttc)
        where `idx` maps entity_id -> row, shared across every root this cycle."""
        _ids, idx, x, edge_index, edge_type = self._build_graph_tensors(node_state, edges)
        with torch.no_grad():
            out = self._model(x, edge_index, edge_type)  # type: ignore[misc]
        if torch.isnan(out["failure_logits"]).any() or torch.isnan(out["ttc"]).any():
            raise RuntimeError("HX-Cascade GNN produced NaN for this cycle's graph")

        # Primary signal: index 2 = the 3600s horizon (03 §4.3 head (a)).
        probs = torch.sigmoid(out["failure_logits"][:, 2]).tolist()
        ttc = out["ttc"].tolist()
        return idx, probs, ttc

    def _predict_gnn(
        self,
        root_entity_id: str,
        node_state: dict[str, dict],
        edges: list[dict],
        max_depth: int | None,
        generated_at: str | None,
    ) -> dict:
        if root_entity_id not in node_state:
            return self._empty_result(root_entity_id, "gnn", generated_at)
        shared = self._run_gnn_forward(node_state, edges)
        return self._extract_gnn_result(root_entity_id, node_state, edges, shared, max_depth, generated_at)

    def _extract_gnn_result(
        self,
        root_entity_id: str,
        node_state: dict[str, dict],
        edges: list[dict],
        shared: tuple[dict[str, int], list[float], list[float]],
        max_depth: int | None,
        generated_at: str | None,
    ) -> dict:
        if root_entity_id not in node_state:
            return self._empty_result(root_entity_id, "gnn", generated_at)

        norm = self._norm
        assert norm is not None
        depth_cap = int(max_depth or self.max_depth)
        cascade_relevant = set(norm.get("cascade_relevant_types", []))
        idx, probs, ttc = shared

        out_edges: dict[str, list[dict]] = {}
        for e in edges:
            out_edges.setdefault(e["src_entity_id"], []).append(e)

        root_state = node_state[root_entity_id]
        root_util = self._forecast_util(root_state)
        root_eta = int(root_state.get("time_to_critical_sec") or 0)
        steps: list[dict] = [
            {
                "entity_id": root_entity_id,
                "predicted_band": _band_from_utilisation(root_util, self.bands),
                "eta_sec": root_eta,
                "failure_probability": round(_clamp(root_util / self.critical, 0.0, 0.99), 3),
                "via_edge_id": None,
                "depth": 0,
            }
        ]

        visited = {root_entity_id}  # structural — prevents cycles, independent of what gets emitted
        frontier: deque[tuple[str, int, int]] = deque([(root_entity_id, 0, root_eta)])
        deepest = 0

        while frontier:
            node, depth, t = frontier.popleft()
            if depth >= depth_cap:
                continue
            for edge in out_edges.get(node, []):
                if edge["edge_type"] not in FLOW_EDGE_TYPES:
                    continue  # substitutes_for is an alternative, not a path load travels
                dst = edge["dst_entity_id"]
                dst_state = node_state.get(dst)
                if dst_state is None or dst in visited or dst not in idx:
                    continue
                visited.add(dst)

                # `t` (the parent's own eta_sec) and the model's `ttc` head are two
                # independently-sourced clocks — the root's eta_sec in particular
                # comes from the forecaster, not this model. Taking a raw `ttc`
                # value can undercut the parent's eta and violate 00 §2.5's
                # ordered-by-eta_sec-ascending contract (root must sort first).
                # max() with the naive additive eta guarantees monotonicity while
                # still letting the model's ttc estimate win when it's larger.
                naive_eta = int(t + int(edge.get("travel_time_sec", 0)))
                dst_ttc = float(ttc[idx[dst]])
                eta = max(naive_eta, int(dst_ttc)) if dst_ttc > 0 else naive_eta
                # Traverse through every node type (context nodes act as bridges
                # in the live graph) even though only relevant types get emitted.
                frontier.append((dst, depth + 1, eta))

                if cascade_relevant and dst_state.get("entity_type") not in cascade_relevant:
                    continue  # message-passing context only, per swap_decision_v1.json — not a trusted output

                prob = _clamp(float(probs[idx[dst]]), 0.0, 0.99)
                band = _band_from_utilisation(prob, self.bands)
                if band not in ("high", "critical") or prob < self.min_probability:
                    continue
                # Physical support: the model's probability alone is not enough to
                # report a failure at an entity that is nowhere near loaded.
                if max(self._forecast_util(dst_state), float(dst_state.get("utilisation", 0.0))) < self.support_util:
                    continue

                steps.append(
                    {
                        "entity_id": dst,
                        "predicted_band": band,
                        "eta_sec": eta,
                        "failure_probability": round(prob, 3),
                        "via_edge_id": edge["edge_id"],
                        "depth": depth + 1,
                    }
                )
                deepest = max(deepest, depth + 1)

        steps = [steps[0]] + sorted(steps[1:], key=lambda s: -s["failure_probability"])[: self.max_steps]
        deepest = max((s["depth"] for s in steps), default=0)
        # Pure value sort — the monotonicity guaranteed above (child eta_sec >=
        # parent eta_sec, transitively >= root) is what keeps the root first,
        # not a forced key, so this matches 00 §2.5 exactly.
        steps.sort(key=lambda s: (s["eta_sec"], s["depth"]))
        for i, s in enumerate(steps):
            s["step_index"] = i

        return {
            "root_entity_id": root_entity_id,
            "source": "gnn",
            "generated_at": generated_at,
            "total_downstream_failures": len(steps) - 1,
            "max_depth": deepest,
            "steps": steps,
        }

    # --- 03 §4.2 deterministic propagator (self-contained fallback) -----------
    def _effective_coefficient(self, edge: dict, dst_state: dict) -> float:
        coeff = float(edge.get("transfer_coefficient", 0.0))
        etype = edge["edge_type"]
        if etype == "feeds":
            return coeff
        if etype == "adjacent_to":
            return coeff * 0.5
        if etype == "serves":
            return coeff if self._forecast_util(dst_state) > 0.7 else 0.0
        if etype == "last_mile_to":
            return coeff
        if etype == "substitutes_for":
            return 0.0
        if etype == "evacuates_to":
            return coeff if self._forecast_util(dst_state) > 0.5 else coeff * 0.5
        return 0.0

    def _propagate_deterministic(
        self,
        root_entity_id: str,
        node_state: dict[str, dict],
        edges: list[dict],
        max_depth: int | None,
        generated_at: str | None,
    ) -> dict:
        depth_cap = int(max_depth or self.max_depth)
        root = node_state.get(root_entity_id)
        if root is None:
            return self._empty_result(root_entity_id, "deterministic", generated_at)

        out_edges: dict[str, list[dict]] = {}
        for e in edges:
            out_edges.setdefault(e["src_entity_id"], []).append(e)

        root_cap = float(root.get("nominal_capacity", 1.0)) or 1.0
        root_forecast = self._forecast_util(root)
        root_eta = int(root.get("time_to_critical_sec") or 0)

        overflow = max(0.0, root_forecast - 1.0) * root_cap
        if overflow <= 0.0:
            overflow = max(0.0, root_forecast - self.critical) * root_cap
        if overflow <= 0.0:
            overflow = 0.02 * root_cap

        steps: list[dict] = [
            {
                "entity_id": root_entity_id,
                "predicted_band": _band_from_utilisation(root_forecast, self.bands),
                "eta_sec": root_eta,
                "failure_probability": round(_clamp(root_forecast / self.critical, 0.0, 0.99), 3),
                "via_edge_id": None,
                "depth": 0,
            }
        ]

        visited = {root_entity_id}
        frontier: deque[tuple[str, int, float, int]] = deque([(root_entity_id, 0, overflow, root_eta)])
        deepest = 0

        while frontier:
            node, depth, load, t = frontier.popleft()
            if depth >= depth_cap:
                continue
            for edge in out_edges.get(node, []):
                if edge["edge_type"] not in FLOW_EDGE_TYPES:
                    continue
                dst = edge["dst_entity_id"]
                dst_state = node_state.get(dst)
                if dst_state is None or dst in visited:
                    continue
                coeff = self._effective_coefficient(edge, dst_state)
                if coeff <= 0.0:
                    continue
                transferred = load * coeff
                dst_cap = float(dst_state.get("nominal_capacity", 1.0)) or 1.0
                if transferred / dst_cap < self.propagation_threshold:
                    continue

                new_util = self._forecast_util(dst_state) + transferred / dst_cap
                band = _band_from_utilisation(new_util, self.bands)
                if band not in ("high", "critical"):
                    continue

                eta = int(t + int(edge.get("travel_time_sec", 0)))
                steps.append(
                    {
                        "entity_id": dst,
                        "predicted_band": band,
                        "eta_sec": eta,
                        "failure_probability": round(_clamp(new_util / self.critical, 0.0, 0.99), 3),
                        "via_edge_id": edge["edge_id"],
                        "depth": depth + 1,
                    }
                )
                visited.add(dst)
                deepest = max(deepest, depth + 1)
                frontier.append((dst, depth + 1, transferred, eta))

        steps = [steps[0]] + sorted(steps[1:], key=lambda s: -s["failure_probability"])[: self.max_steps]
        deepest = max((s["depth"] for s in steps), default=0)
        steps.sort(key=lambda s: (s["depth"] > 0, s["eta_sec"], s["depth"]))
        for i, s in enumerate(steps):
            s["step_index"] = i

        return {
            "root_entity_id": root_entity_id,
            "source": "deterministic",
            "generated_at": generated_at,
            "total_downstream_failures": len(steps) - 1,
            "max_depth": deepest,
            "steps": steps,
        }
