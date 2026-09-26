"""CascadePredictor — HX-Cascade GNN (03_ML_CONTRACT.md §4).

Drop-in per `ML/README.md`'s contract: `Backend/app/ml_registry.py` imports this
module in place of `app.ml_reference.cascade` whenever this file exists;
`use_gnn: true` in `Backend/config.yaml` makes it load the checkpoint.

How the backend uses it: the published cascade is always the deterministic
flow cascade (`Backend/app/services/cascade_flow.py`). This model's per-entity
failure probabilities are either kept for evaluation only (`cascade.gnn_mode:
shadow`, the default) or attached to non-root cascade steps as `confidence`
(`annotate`). Root steps' `failure_probability` here is utilisation / critical
line, not a model output, and is never published as confidence.

Evidence for the current bundle (`artifacts/hx_cascade_v2/eval.json`): held-out
*scenarios* on the same map it was trained on, compared with a
forecast-threshold rule. That is not the 03 §4.3 swap criterion (held-out
topologies, compared with the deterministic propagator), hence shadow mode.
`swap_decision_v1.json` evaluates the retired v1 checkpoint (32-entity map) and
does not apply to v2.

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

import numpy as np
import torch

from .models.hx_cascade import HXCascade, build_model  # noqa: F401  (HXCascade: v2 training script imports it from here)

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
    """The most severe roots only: critical now, or forecast to cross critical
    (whatever the current band, so a cascade is predicted before it starts). Ranked by risk score then projected load; capped at `limit`."""
    def projected(st: dict) -> float:
        return max(forecast_util(st), float(st.get("utilisation", 0.0)))

    roots = [
        eid for eid, st in node_state.items()
        if st.get("risk_band") == "critical" or projected(st) >= critical
    ]
    roots.sort(key=lambda e: (-int(node_state[e].get("risk_score", 0)), -projected(node_state[e]), e))
    return roots[:limit]


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
        self._manifest: dict[str, Any] | None = None
        self._temperature: dict[int, float] = {}
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
            "trained_on": m.get("trained_on"),
            "evaluated_outputs": m.get("evaluated_outputs", []),
            "error": self._load_error,
        }

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

            self._model, self._norm, self._manifest = model, norm, manifest
            self._v3 = arch.get("class") == "HXCascadeV3"
            self._temperature, self._bundle = temperature, bundle
            self._sanity_check_dummy_forward()
            self._gnn_ready = True
            log.info("HX-Cascade GNN %s loaded from %s", self.model_version(), bundle)
        except Exception as exc:
            log.exception("HX-Cascade GNN failed to load; running deterministic-only")
            self._model = None
            self._norm = None
            self._manifest = None
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

    def _forward_all(self, node_state: dict[str, dict], edges: list[dict]) -> tuple[dict[str, int], np.ndarray, np.ndarray]:
        """One forward pass over the whole graph -> (entity index, P(cross by h) (N, 3), ttc_sec (N,)).
        Calibrated with the bundle's temperatures when present, monotone across horizons."""
        if self._v3:
            from .features.graph_features import build

            g = build(node_state, edges, self._thresholds())
            idx = {e: i for i, e in enumerate(g["entity_ids"])}
            with torch.no_grad():
                out = self._model(torch.from_numpy(g["x"]), torch.from_numpy(g["edge_index"]),  # type: ignore[misc]
                                  torch.from_numpy(g["edge_attr"]))
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
        return idx, p, ttc

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

    HORIZONS = (900, 1800, 3600)

    def node_risk(
        self,
        node_state: dict[str, dict],
        edges: list[dict],
        generated_at: str | None = None,
    ) -> dict:
        """Per-entity failure probability at every horizon plus time-to-critical,
        from ONE forward pass over the whole graph. Only the cascade-relevant
        entity types (the ones the evaluation covers) are reported.

            {"source": "gnn", "model_version": "hx_cascade_v2@745b2205",
             "generated_at": ..., "calibrated": bool, "topology_match": bool,
             "nodes": {eid: {"p_fail_900", "p_fail_1800", "p_fail_3600", "ttc_sec"}}}

        `calibrated` is False unless the bundle ships calibration.json: the
        probabilities are then raw sigmoid outputs of a model trained with class
        weighting, and should not be read as frequencies. `ttc_sec` comes from a
        head that has not been evaluated (see `model_info()["evaluated_outputs"]`).
        """
        if not (self.use_gnn and self._gnn_ready):
            return self.node_risk_fallback(node_state, edges, generated_at)
        try:
            from .manifest import topology_hash

            idx, probs, ttc_arr = self._forward_all(node_state, edges)
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
            expected = (self._manifest or {}).get("topology_hash")
            return {
                "source": "gnn",
                "model_version": self.model_version(),
                "generated_at": generated_at,
                "calibrated": bool(self._temperature),
                "topology_match": None if not expected else topology_hash(node_state, edges) == expected,
                "nodes": nodes,
            }
        except Exception:
            log.exception("HX-Cascade node_risk failed; falling back")
            return self.node_risk_fallback(node_state, edges, generated_at)

    def node_risk_fallback(self, node_state: dict[str, dict], edges: list[dict],
                           generated_at: str | None = None) -> dict:
        """Same shape as `node_risk`, with no model output (the deterministic
        cascade carries on without probabilities)."""
        return {"source": "deterministic", "model_version": None, "generated_at": generated_at,
                "calibrated": False, "topology_match": None, "nodes": {}}

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

    def _run_gnn_forward(self, node_state: dict[str, dict], edges: list[dict]) -> tuple[dict[str, int], list[float], list[float]]:
        """One forward pass for the whole live graph. Returns (idx, probs, ttc)
        where `idx` maps entity_id -> row, shared across every root this cycle.
        Primary signal: the 3600s horizon (03 §4.3 head (a))."""
        idx, probs, ttc = self._forward_all(node_state, edges)
        return idx, probs[:, 2].tolist(), ttc.tolist()

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
