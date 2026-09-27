"""CascadePredictor — HX-Cascade GNN (03_ML_CONTRACT.md §4).

Drop-in per `ML/README.md`'s contract: `Backend/app/ml_registry.py` imports this
module in place of `app.ml_reference.cascade` whenever this file (and torch)
import; `use_gnn: true` in `Backend/config.yaml` makes it load the bundle.

What it does: `node_risk()` — per-entity P(cross the critical line by 900 /
1800 / 3600 s) and time-to-critical from one forward pass over the whole graph.
It does not build cascades. The published cascade is always the backend's
deterministic flow cascade (`Backend/app/services/cascade_flow.py`, the one
propagator); these probabilities are either kept for evaluation only
(`cascade.gnn_mode: shadow`) or attached to its non-root steps as `confidence`
(`annotate`). `predict` / `predict_all` / `fallback` keep the 03 §4.1 interface
and return no cascade steps: a copy of the propagator here would be a second,
drifting definition.

Out-of-distribution guard: `node_risk()` falls back (`source: "deterministic"`,
no probabilities, `fallback_reason` says why) when
  * the bundle names a training topology (`manifest.topology_hash`) and this
    graph is not it (v2: trained on one map); or
  * the bundle ships `ood_stats.json` (v3: train-map statistics) and, over the
    scored entities (cascade-relevant types not already over their line: the
    population the model was trained and evaluated on), any feature lies outside
    the train range (± `ood_guard.range_tolerance` of that range), or more than
    `ood_guard.max_embedding_frac` of them sit beyond the train embeddings' p99
    distance.
Every result carries the `ood` diagnostics that decided it.

No I/O after `__init__` (03 §0 rule 1): the bundle loads once at construction.
No imports from `Backend/` (03 §0 rule 2).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import numpy as np
import torch

from .models.hx_cascade import HXCascade, build_model  # noqa: F401  (HXCascade: re-exported for older callers)

log = logging.getLogger("eventflow.ml.cascade")

_HERE = Path(__file__).resolve().parent
_REPO_ROOT_GUESS = _HERE.parent


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


class CascadePredictor:
    def __init__(self, config: dict) -> None:
        self.config = config or {}
        self.seed = int(self.config.get("seed", 42))
        self.use_gnn = bool(self.config.get("use_gnn", False))
        self.critical = float(self.config.get("critical_utilisation", 0.90))
        self.min_probability = float(self.config.get("gnn_min_probability", 0.6))
        guard = self.config.get("ood_guard") or {}
        self.ood_enabled = bool(guard.get("enabled", True))
        self.ood_range_tolerance = float(guard.get("range_tolerance", 0.05))
        self.ood_max_embedding_frac = float(guard.get("max_embedding_frac", 0.5))

        self._model: HXCascade | None = None
        self._norm: dict[str, Any] | None = None
        self._manifest: dict[str, Any] | None = None
        self._temperature: dict[int, float] = {}
        self._alert_thresholds: dict[int, float] = {}
        self._ood: dict[str, Any] | None = None
        self._bundle: Path | None = None
        self._gnn_ready = False
        self._v3 = False
        self._load_error: str | None = None
        if self.use_gnn:
            self._load_gnn()

    def ready(self) -> bool:
        """False when the model was asked for (`use_gnn`) but its verified bundle
        could not be loaded; the backend then runs without it."""
        return self._gnn_ready if self.use_gnn else True

    def active_source(self) -> str:
        return "gnn" if (self.use_gnn and self._gnn_ready) else "deterministic"

    def model_info(self) -> dict[str, Any]:
        """Identity of the loaded model, for `/health` and persisted predictions."""
        m = self._manifest or {}
        files = m.get("files") or {}
        return {
            "model_version": self.model_version(),
            "ready": self.ready(),
            "bundle": str(self._bundle) if self._bundle else None,
            "checkpoint_sha256": (files.get("model.pt") or {}).get("sha256"),
            "norm_sha256": (files.get("feature_norm.json") or {}).get("sha256"),
            "topology_hash": m.get("topology_hash"),
            "calibrated": bool(self._temperature),
            "alert_thresholds": {str(h): self.alert_threshold(h) for h in self.HORIZONS},
            "trained_on": m.get("trained_on"),
            "evaluated_outputs": m.get("evaluated_outputs", []),
            "ood_guard": {"enabled": self.ood_enabled, "topology_bound": bool(m.get("topology_hash")),
                          "feature_stats": self._ood is not None},
            "error": self._load_error,
        }

    def alert_threshold(self, horizon_sec: int) -> float:
        """P(cross by h) at or above which the model is counted as alerting: the
        bundle's evaluated operating point, else config `gnn_min_probability`."""
        return self._alert_thresholds.get(int(horizon_sec), self.min_probability)

    def model_version(self) -> str | None:
        """`<model_version>@<first 8 hex of the checkpoint hash>`, None when not loaded."""
        if not self._gnn_ready or not self._manifest:
            return None
        sha = ((self._manifest.get("files") or {}).get("model.pt") or {}).get("sha256", "")
        return f"{self._manifest.get('model_version', 'unknown')}@{sha[:8]}"

    # --- model loading (init only — 03 §0 rule 1) ---------------------------
    def _resolve_bundle(self) -> Path:
        configured = self.config.get("gnn_artifact")
        if not configured:
            raise FileNotFoundError("cascade.gnn_artifact is not set (a model bundle directory with manifest.json)")
        for candidate in (Path(configured), _REPO_ROOT_GUESS / configured):
            if candidate.is_dir():
                return candidate
        raise FileNotFoundError(f"model bundle {configured} not found")

    def _load_gnn(self) -> None:
        try:
            from .manifest import load_manifest

            torch.manual_seed(self.seed)
            bundle = self._resolve_bundle()
            manifest = load_manifest(bundle)   # verifies every file's sha256
            norm = json.loads((bundle / "feature_norm.json").read_text(encoding="utf-8"))
            arch = manifest.get("architecture") or {}
            if int(arch.get("node_feat_dim", norm["node_feat_dim"])) != int(norm["node_feat_dim"]):
                raise ValueError("manifest architecture and feature_norm.json disagree on node_feat_dim")

            model = build_model({**arch, "node_feat_dim": int(norm["node_feat_dim"])})
            state_dict = torch.load(bundle / "model.pt", map_location="cpu", weights_only=True)
            model.load_state_dict(state_dict)
            model.eval()

            temperature: dict[int, float] = {}
            if "calibration.json" in (manifest.get("files") or {}):
                cal = json.loads((bundle / "calibration.json").read_text(encoding="utf-8"))
                temperature = {int(h): float(t) for h, t in (cal.get("temperature") or {}).items()}

            # The alert thresholds the bundle was evaluated at (eval.json
            # `operating_points`), so online evaluation scores the same operating
            # point the offline numbers describe.
            alert: dict[int, float] = {}
            if "eval.json" in (manifest.get("files") or {}):
                ev = json.loads((bundle / "eval.json").read_text(encoding="utf-8"))
                op = ((ev.get("operating_points") or {}).get(manifest.get("model_version")) or {})
                alert = {int(h): float(t) for h, t in (op.get("thresholds") or {}).items()}

            ood = None
            if "ood_stats.json" in (manifest.get("files") or {}):
                ood = json.loads((bundle / "ood_stats.json").read_text(encoding="utf-8"))
                if len(ood.get("feature_min") or []) != int(norm["node_feat_dim"]):
                    raise ValueError("ood_stats.json and feature_norm.json disagree on the feature layout")

            self._model, self._norm, self._manifest, self._ood = model, norm, manifest, ood
            self._v3 = arch.get("class") == "HXCascadeV3"
            self._temperature, self._bundle, self._alert_thresholds = temperature, bundle, alert
            self._sanity_check_dummy_forward()
            self._gnn_ready = True
            log.info("HX-Cascade GNN %s loaded from %s", self.model_version(), bundle)
        except Exception as exc:
            log.exception("HX-Cascade GNN failed to load; running deterministic-only")
            self._model = None
            self._norm = None
            self._manifest = None
            self._ood = None
            self._gnn_ready = False
            self._load_error = f"{type(exc).__name__}: {exc}"

    def _sanity_check_dummy_forward(self) -> None:
        """A silent feature-mismatch must fail loudly at boot, not produce
        garbage predictions in the demo (this is exactly how the training-time
        leakage bug first showed up)."""
        assert self._model is not None and self._norm is not None
        x = torch.zeros((4, int(self._norm["node_feat_dim"])), dtype=torch.float32)
        edge_index = torch.tensor([[0, 1, 2], [1, 2, 3]], dtype=torch.long)
        with torch.no_grad():
            if self._v3:
                out = self._model(x, edge_index, torch.zeros((3, int(self._norm["edge_feat_dim"])), dtype=torch.float32))
            else:
                out = self._model(x, edge_index, torch.tensor([0, 0, 0], dtype=torch.long))
        if torch.isnan(out["failure_logits"]).any() or torch.isnan(out["ttc"]).any():
            raise RuntimeError("HX-Cascade GNN sanity forward pass produced NaN")

    def _thresholds(self) -> dict[str, Any]:
        """The config `thresholds` the v3 features and labels are defined against."""
        return {"critical_utilisation": self.critical, "by_type": self.config.get("thresholds_by_type") or {}}


    def _forward_all(self, node_state: dict[str, dict], edges: list[dict]
                     ) -> tuple[dict[str, int], np.ndarray, np.ndarray, dict | None]:
        """One forward pass over the whole graph -> (entity index, P(cross by h) (N, 3),
        ttc_sec (N,), the inputs the OOD guard reads (v3; None for v2)).
        Calibrated with the bundle's temperatures when present, monotone across horizons."""
        inputs = None
        if self._v3:
            from .features.graph_features import build

            g = build(node_state, edges, self._thresholds())
            idx = {e: i for i, e in enumerate(g["entity_ids"])}
            with torch.no_grad():
                out = self._model(torch.from_numpy(g["x"]), torch.from_numpy(g["edge_index"]),  # type: ignore[misc]
                                  torch.from_numpy(g["edge_attr"]))
            inputs = {"x": g["x"], "types": g["types"], "critical": g["critical"],
                      "embedding": out["embedding"].numpy() if "embedding" in out else None}
        else:
            _ids, idx, x, edge_index, edge_type = self._build_graph_tensors(node_state, edges)
            with torch.no_grad():
                out = self._model(x, edge_index, edge_type)  # type: ignore[misc]
        logits, ttc = out["failure_logits"].numpy(), out["ttc"].numpy()
        if np.isnan(logits).any() or np.isnan(ttc).any():
            raise RuntimeError("HX-Cascade GNN produced NaN for this graph")
        temps = np.array([self._temperature.get(h, 1.0) for h in self.HORIZONS])
        p = 1.0 / (1.0 + np.exp(-logits / temps[None, :]))
        if self._v3 and self._temperature:
            # v3 is monotone across horizons by construction; per-horizon
            # temperatures must not undo that (same as training's `calibrated`).
            p = np.maximum.accumulate(p, axis=-1)
        return idx, p, ttc, inputs

    # --- out-of-distribution guard -----------------------------------------------
    def _ood_check(self, inputs: dict | None) -> tuple[str | None, dict[str, Any]]:
        """(reason to refuse this graph, or None; diagnostics) against ood_stats.json."""
        stats = self._ood
        if not (self.ood_enabled and stats and inputs is not None):
            return None, {"checked": False}
        x, crit = inputs["x"], inputs["critical"]
        relevant = list((self._norm or {}).get("cascade_relevant_types") or [])
        scored = np.isin(np.asarray(inputs["types"]), relevant) & (x[:, 0] < crit)
        diag: dict[str, Any] = {"checked": True, "scored_entities": int(scored.sum()), "out_of_range_entities": 0,
                                "out_of_range_features": [], "embedding_beyond_p99_frac": 0.0}
        if not scored.any():
            return None, diag
        lo, hi = np.asarray(stats["feature_min"]), np.asarray(stats["feature_max"])
        slack = self.ood_range_tolerance * (hi - lo) + 1e-3
        bad = (x[scored] < lo - slack) | (x[scored] > hi + slack)
        names = stats.get("feature_order") or [f"f{j}" for j in range(len(lo))]
        diag["out_of_range_entities"] = int(bad.any(axis=1).sum())
        diag["out_of_range_features"] = [names[j] for j in np.where(bad.any(axis=0))[0]]
        emb = inputs.get("embedding")
        if emb is not None and stats.get("embedding_mean") is not None:
            z = (emb[scored] - np.asarray(stats["embedding_mean"])) / np.asarray(stats["embedding_std"])
            dist = np.sqrt((z ** 2).mean(axis=1))
            diag["embedding_beyond_p99_frac"] = round(float((dist > float(stats["embedding_distance_p99"])).mean()), 4)
        if diag["out_of_range_entities"]:
            return (f"features_out_of_range: {diag['out_of_range_entities']} of {diag['scored_entities']} scored "
                    f"entities outside the training range ({', '.join(diag['out_of_range_features'])})"), diag
        if diag["embedding_beyond_p99_frac"] > self.ood_max_embedding_frac:
            return (f"embedding_distance: {diag['embedding_beyond_p99_frac']:.0%} of scored entities beyond the "
                    f"training p99 (limit {self.ood_max_embedding_frac:.0%})"), diag
        return None, diag

    HORIZONS = (900, 1800, 3600)

    # --- what the backend calls (03 §4.5) -----------------------------------------
    def node_risk(
        self,
        node_state: dict[str, dict],
        edges: list[dict],
        generated_at: str | None = None,
    ) -> dict:
        """Per-entity failure probability at every horizon plus time-to-critical,
        from ONE forward pass over the whole graph. Only the cascade-relevant
        entity types (the ones the evaluation covers) are reported.

            {"source": "gnn", "model_version": "hx_cascade_v3@1ff98e87",
             "generated_at": ..., "calibrated": bool, "topology_match": bool | None,
             "fallback_reason": None, "ood": {...},
             "nodes": {eid: {"p_fail_900", "p_fail_1800", "p_fail_3600", "ttc_sec"}}}

        `calibrated` is False unless the bundle ships calibration.json: the
        probabilities are then raw sigmoid outputs of a model trained with class
        weighting, and should not be read as frequencies. A graph the OOD guard
        refuses gets `node_risk_fallback()` with its `fallback_reason`.
        """
        if not (self.use_gnn and self._gnn_ready):
            return self.node_risk_fallback(node_state, edges, generated_at)
        try:
            from .manifest import topology_hash

            expected = (self._manifest or {}).get("topology_hash")
            match = None if not expected else topology_hash(node_state, edges) == expected
            if match is False and self.ood_enabled:
                return self.node_risk_fallback(node_state, edges, generated_at, topology_match=False,
                                               reason="topology_hash_mismatch: not the map this model was trained on")
            idx, probs, ttc_arr, inputs = self._forward_all(node_state, edges)
            reason, diag = self._ood_check(inputs)
            if reason:
                return self.node_risk_fallback(node_state, edges, generated_at, topology_match=match,
                                               reason=reason, ood=diag)
            columns = [probs[:, j].tolist() for j in range(len(self.HORIZONS))]
            ttc_list = ttc_arr.tolist()
            relevant = set((self._norm or {}).get("cascade_relevant_types", []))
            nodes: dict[str, dict] = {}
            for eid, i in idx.items():
                if relevant and node_state[eid].get("entity_type") not in relevant:
                    continue
                nodes[eid] = {
                    **{f"p_fail_{h}": round(columns[j][i], 4) for j, h in enumerate(self.HORIZONS)},
                    "ttc_sec": int(round(_clamp(float(ttc_list[i]), 0.0, float(self.HORIZONS[-1])))),
                }
            return {
                "source": "gnn",
                "model_version": self.model_version(),
                "generated_at": generated_at,
                "calibrated": bool(self._temperature),
                "topology_match": match,
                "fallback_reason": None,
                "ood": diag,
                "nodes": nodes,
            }
        except Exception:
            log.exception("HX-Cascade node_risk failed; falling back")
            return self.node_risk_fallback(node_state, edges, generated_at)

    def node_risk_fallback(self, node_state: dict[str, dict], edges: list[dict],
                           generated_at: str | None = None, *, topology_match: bool | None = None,
                           reason: str | None = None, ood: dict | None = None) -> dict:
        """Same shape as `node_risk`, with no model output (the deterministic
        cascade carries on without probabilities). `reason`: why the model was
        not used for this graph (the OOD guard); None when it is simply not loaded."""
        return {"source": "deterministic", "model_version": None, "generated_at": generated_at,
                "calibrated": False, "topology_match": topology_match, "fallback_reason": reason,
                "ood": ood or {"checked": False}, "nodes": {}}

    # --- 03 §4.1 interface; cascades are built by the backend ---------------------
    def predict(
        self,
        root_entity_id: str,
        node_state: dict[str, dict],
        edges: list[dict],
        max_depth: int | None = None,
        generated_at: str | None = None,
    ) -> dict:
        """No cascade structure: the backend's `cascade_flow` builds every published
        cascade. Returns an empty, schema-valid CascadeResult."""
        return self.fallback(root_entity_id, node_state, edges, max_depth, generated_at)

    def predict_all(self, node_state: dict[str, dict], edges: list[dict], generated_at: str | None = None) -> list[dict]:
        """No cascades (see `predict`); the backend reads this model through `node_risk`."""
        return []

    def fallback(
        self,
        root_entity_id: str,
        node_state: dict[str, dict],
        edges: list[dict],
        max_depth: int | None = None,
        generated_at: str | None = None,
    ) -> dict:
        return {
            "root_entity_id": root_entity_id,
            "source": "deterministic",
            "generated_at": generated_at,
            "total_downstream_failures": 0,
            "max_depth": 0,
            "steps": [],
        }

    # --- v2 features (v3: ML/features/graph_features.py) ----------------------
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
            if norm.get("forecast_features"):
                # The live forecaster's projections (twin model + bias
                # correction), exactly as built for training.
                for j, h in enumerate((900, 1800, 3600)):
                    fv = st.get(f"forecast_{h}")
                    x[i, 3 + n_types + j] = min(float(util if fv is None else fv), util_clip)

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
