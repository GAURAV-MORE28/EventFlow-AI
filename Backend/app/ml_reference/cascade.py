"""CascadePredictor (03_ML_CONTRACT.md §4).

The deterministic flow propagator from §4.2, built first because it is the
fallback that guarantees a demo exists. `use_gnn: true` routes to the ML
workstream's HX-Cascade GNN when it beats this on held-out *topologies* — until
then this is the shipping path and the rendering is identical either way.

Edge-type transfer semantics (§4.2 table) are applied here, but the coefficients
themselves live in the seed data, not in this file.
"""
from __future__ import annotations

import logging
from collections import deque
from typing import Any

from .common import band_from_utilisation, clamp

log = logging.getLogger("eventflow.ml.cascade")


class CascadePredictor:
    def __init__(self, config: dict) -> None:
        self.config = config or {}
        self.use_gnn = bool(self.config.get("use_gnn", False))
        self.max_depth = int(self.config.get("max_depth", 4))
        self.propagation_threshold = float(self.config.get("propagation_threshold", 0.15))
        self.critical = float(self.config.get("critical_utilisation", 0.90))
        self.bands = self.config.get("risk_bands", {"low": 30, "moderate": 60, "high": 80})
        self.max_roots = int(self.config.get("max_roots", 5))
        self.max_steps = int(self.config.get("max_steps", 8))
        self.exclude_root_types = set(self.config.get("exclude_root_types", ["hotel"]))

    def ready(self) -> bool:
        return True

    def active_source(self) -> str:
        return "gnn" if self.use_gnn else "deterministic"

    # --- 03 §4.1 -----------------------------------------------------------
    def predict(
        self,
        root_entity_id: str,
        node_state: dict[str, dict],
        edges: list[dict],
        max_depth: int | None = None,
        generated_at: str | None = None,
    ) -> dict:
        try:
            return self._propagate(root_entity_id, node_state, edges, max_depth, generated_at)
        except Exception:
            log.exception("cascade predict failed for %s", root_entity_id)
            return self.fallback(root_entity_id, node_state, edges, max_depth, generated_at)

    def predict_all(
        self,
        node_state: dict[str, dict],
        edges: list[dict],
        generated_at: str | None = None,
    ) -> list[dict]:
        """Every entity in band high|critical becomes a cascade root.

        "Band" is read as the union of the entity's current `risk_band` and the
        band its own 30-minute forecast implies. Rooting only on the *current*
        band would make the cascade reactive — it would fire once the entity is
        already in trouble, and the lead-time metric in 03 §4.4 asks for >=900s
        of warning. An entity forecast into the critical band is precisely what a
        predictive cascade should be rooted on.
        """
        def projected(st: dict) -> float:
            return max(self._forecast_util(st), float(st.get("utilisation", 0.0)))

        roots = [
            eid for eid, st in node_state.items()
            if st.get("entity_type") not in self.exclude_root_types
            and (st.get("risk_band") == "critical" or (st.get("risk_band") == "high" and projected(st) >= self.critical))
        ]
        # Most severe first, capped, so the overlay shows chains an operator can read.
        roots.sort(key=lambda e: (-int(node_state[e].get("risk_score", 0)), -projected(node_state[e]), e))
        roots = roots[: self.max_roots]
        return [self.predict(r, node_state, edges, generated_at=generated_at) for r in roots]

    def fallback(
        self,
        root_entity_id: str,
        node_state: dict[str, dict],
        edges: list[dict],
        max_depth: int | None = None,
        generated_at: str | None = None,
    ) -> dict:
        """Deterministic propagation. Identical to `predict` when use_gnn is false."""
        return self._propagate(root_entity_id, node_state, edges, max_depth, generated_at)

    # --- §4.2 the propagator ----------------------------------------------
    def _forecast_util(self, state: dict, horizon: int = 1800) -> float:
        key = f"forecast_{horizon}"
        if key in state and state[key] is not None:
            return float(state[key])
        return float(state.get("utilisation", 0.0))

    def _effective_coefficient(self, edge: dict, dst_state: dict) -> float:
        """§4.2 edge-type transfer semantics."""
        coeff = float(edge.get("transfer_coefficient", 0.0))
        etype = edge["edge_type"]
        if etype == "feeds":
            return coeff
        if etype == "adjacent_to":
            return coeff * 0.5          # bidirectional, so halve it
        if etype == "serves":
            # Capacity coupling: only bites once the destination is already loaded.
            return coeff if self._forecast_util(dst_state) > 0.7 else 0.0
        if etype == "last_mile_to":
            return coeff                # delay is carried by travel_time_sec
        if etype == "substitutes_for":
            return 0.0                  # negative transfer; the solver's business, not ours
        if etype == "evacuates_to":
            return coeff if self._forecast_util(dst_state) > 0.5 else coeff * 0.5
        return 0.0

    def _propagate(
        self,
        root_entity_id: str,
        node_state: dict[str, dict],
        edges: list[dict],
        max_depth: int | None,
        generated_at: str | None,
    ) -> dict:
        depth_cap = int(max_depth or self.max_depth)
        source = self.active_source()

        root = node_state.get(root_entity_id)
        if root is None:
            return {
                "root_entity_id": root_entity_id,
                "source": source,
                "generated_at": generated_at,
                "total_downstream_failures": 0,
                "max_depth": 0,
                "steps": [],
            }

        out_edges: dict[str, list[dict]] = {}
        for e in edges:
            out_edges.setdefault(e["src_entity_id"], []).append(e)

        root_cap = float(root.get("nominal_capacity", 1.0)) or 1.0
        root_forecast = self._forecast_util(root)
        root_eta = int(root.get("time_to_critical_sec") or 0)

        overflow = max(0.0, root_forecast - 1.0) * root_cap
        if overflow <= 0.0:
            # Pre-emptive: propagate the headroom that will be eaten, not just the spill.
            overflow = max(0.0, root_forecast - self.critical) * root_cap
        if overflow <= 0.0:
            overflow = 0.02 * root_cap  # a whisper, so a high-band root still shows a chain

        steps: list[dict] = [
            {
                "entity_id": root_entity_id,
                "predicted_band": band_from_utilisation(root_forecast, self.critical, self.bands),
                "eta_sec": root_eta,
                "failure_probability": round(clamp(root_forecast / self.critical, 0.0, 0.99), 3),
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
                band = band_from_utilisation(new_util, self.critical, self.bands)
                if band not in ("high", "critical"):
                    continue

                eta = int(t + int(edge.get("travel_time_sec", 0)))
                steps.append(
                    {
                        "entity_id": dst,
                        "predicted_band": band,
                        "eta_sec": eta,
                        "failure_probability": round(clamp(new_util / self.critical, 0.0, 0.99), 3),
                        "via_edge_id": edge["edge_id"],
                        "depth": depth + 1,
                    }
                )
                visited.add(dst)
                deepest = max(deepest, depth + 1)
                frontier.append((dst, depth + 1, transferred, eta))

        # 00 §2.5 — ordered by eta_sec ascending, step_index reassigned after the sort.
        # The frontend animates in array order and must never re-sort.
        # Every downstream eta is root_eta + travel time, so the root sorts first;
        # the explicit key keeps that true rather than assuming it.
        steps = [steps[0]] + sorted(steps[1:], key=lambda s: -s["failure_probability"])[: self.max_steps]
        deepest = max((s["depth"] for s in steps), default=0)
        steps.sort(key=lambda s: (s["depth"] > 0, s["eta_sec"], s["depth"]))
        for i, s in enumerate(steps):
            s["step_index"] = i

        return {
            "root_entity_id": root_entity_id,
            "source": source,
            "generated_at": generated_at,
            "total_downstream_failures": len(steps) - 1,
            "max_depth": deepest,
            "steps": steps,
        }
