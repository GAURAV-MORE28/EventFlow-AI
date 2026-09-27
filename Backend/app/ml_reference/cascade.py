"""CascadePredictor reference (03_ML_CONTRACT.md §4).

Used when `ML/cascade.py` (or torch) is not available. It has no model, so it
builds cascades with the backend's one deterministic propagator
(`services/cascade_flow.py`, the same code that builds every published cascade)
instead of keeping a copy of it. `node_risk()` reports no probabilities.

The engine publishes `cascade_flow` cascades directly and only reads a cascade
module through `node_risk()`; this class exists so every module has a working
reference with the contract's shapes (03 §0).
"""
from __future__ import annotations

from typing import Any


class CascadePredictor:
    def __init__(self, config: dict) -> None:
        self.config = config or {}
        self.use_gnn = bool(self.config.get("use_gnn", False))
        critical = float(self.config.get("critical_utilisation", 0.90))
        warning = float(self.config.get("warning_utilisation", 0.75))
        by_type = self.config.get("thresholds_by_type") or {}

        def lines(entity_type: str | None) -> tuple[float, float]:
            t = by_type.get(entity_type or "", {})
            return float(t.get("warning", warning)), float(t.get("critical", critical))

        self._lines = lines

    def ready(self) -> bool:
        return True

    def active_source(self) -> str:
        return "deterministic"   # no model here, whatever `use_gnn` says

    def model_info(self) -> dict[str, Any]:
        return {"model_version": None, "ready": True, "bundle": None, "checkpoint_sha256": None,
                "norm_sha256": None, "topology_hash": None, "calibrated": False, "trained_on": None,
                "evaluated_outputs": [], "error": None}

    # --- 03 §4.1 -----------------------------------------------------------
    def predict(
        self,
        root_entity_id: str,
        node_state: dict[str, dict],
        edges: list[dict],
        max_depth: int | None = None,
        generated_at: str | None = None,
    ) -> dict:
        from ..services.cascade_flow import cascade_for

        if root_entity_id not in node_state:
            return self._empty(root_entity_id, generated_at)
        cfg = {**self.config, "max_depth": int(max_depth or self.config.get("max_depth", 4))}
        return cascade_for(root_entity_id, node_state, edges, self._lines, cfg, generated_at)  # type: ignore[arg-type]

    def predict_all(
        self,
        node_state: dict[str, dict],
        edges: list[dict],
        generated_at: str | None = None,
    ) -> list[dict]:
        from ..services.cascade_flow import build_cascades

        return build_cascades(node_state, edges, self._lines, self.config, generated_at)  # type: ignore[arg-type]

    def fallback(
        self,
        root_entity_id: str,
        node_state: dict[str, dict],
        edges: list[dict],
        max_depth: int | None = None,
        generated_at: str | None = None,
    ) -> dict:
        try:
            return self.predict(root_entity_id, node_state, edges, max_depth, generated_at)
        except Exception:
            return self._empty(root_entity_id, generated_at)

    # --- 03 §4.5 -----------------------------------------------------------
    def node_risk(self, node_state: dict[str, dict], edges: list[dict], generated_at: str | None = None) -> dict:
        return self.node_risk_fallback(node_state, edges, generated_at)

    def node_risk_fallback(self, node_state: dict[str, dict], edges: list[dict],
                           generated_at: str | None = None) -> dict:
        return {"source": "deterministic", "model_version": None, "generated_at": generated_at,
                "calibrated": False, "topology_match": None, "fallback_reason": None,
                "ood": {"checked": False}, "nodes": {}}

    @staticmethod
    def _empty(root_entity_id: str, generated_at: str | None) -> dict:
        return {"root_entity_id": root_entity_id, "source": "deterministic", "generated_at": generated_at,
                "total_downstream_failures": 0, "max_depth": 0, "steps": []}
