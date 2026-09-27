"""Deterministic flow cascade — the backend's own, ML-independent cascade.

A cascade starts at an entity that is (or is forecast within 30 minutes to be)
over its critical line. The people above that line — its *overflow* — cannot be
served there; they are split over **every** outbound flow edge of the entity in
proportion to the edge's transfer coefficient (no path is chosen over another,
and no people are invented: the shares sum to the overflow). A downstream entity
is affected only if its own projected load plus the share it receives reaches
its warning line; it then passes on only what exceeds *its* critical line, so a
cascade continues exactly as far as the physics says and stops where there is
spare capacity.

Inputs are the authoritative state (utilisation, the published forecasts, the
per-type thresholds, the topology). ML is optional: when a cascade model has
produced per-entity failure probabilities they are attached to matching steps
as `confidence`; with no model the cascade is identical except that field is
null. Everything here is deterministic.
"""
from __future__ import annotations

from collections import deque
from typing import Any, Callable

from ..ml_reference.generator import (
    EMERGENCY_GAIN, EMERGENCY_MAX, EMERGENCY_TRIGGER_UTIL, GATE_HOLD_FACTOR, LINE_MAX, ROAD_MAX, STATION_MAX, ZONE_MAX,
)

# Edges along which *people* move (the overflow is split over these).
PEOPLE_EDGE_TYPES = ("feeds", "adjacent_to", "serves", "last_mile_to")
# `evacuates_to` is an impact coupling (crowding upstream raises incident load
# at the emergency post); nobody walks into the post. Same formula as the simulator.
IMPACT_EDGE_TYPES = ("evacuates_to",)
FLOW_EDGE_TYPES = PEOPLE_EDGE_TYPES + IMPACT_EDGE_TYPES
# The simulator's physical ceilings: load beyond them stays queued upstream.
PHYSICAL_MAX = {"road": ROAD_MAX, "transport_node": STATION_MAX, "transport_route": LINE_MAX, "zone": ZONE_MAX,
                "emergency_facility": EMERGENCY_MAX, "gate": GATE_HOLD_FACTOR, "venue": 1.0}
PROJECTION_KEYS = ("forecast_900", "forecast_1800")
MIN_ROOT_OVERFLOW = 0.02  # a root exactly on its line still displaces a little load


def projected(state: dict) -> float:
    """Highest of now and the next 30 minutes of the published forecast."""
    vals = [float(state.get("utilisation", 0.0))]
    vals += [float(state[k]) for k in PROJECTION_KEYS if state.get(k) is not None]
    return max(vals)


def _band(util: float, warning: float, critical: float) -> str | None:
    if util >= critical:
        return "critical"
    if util >= warning:
        return "high"
    return None


def select_roots(node_state: dict[str, dict], lines: Callable[[str], tuple[float, float]],
                 exclude_types: set[str], closed: set[str], limit: int) -> list[str]:
    roots = []
    for eid, st in node_state.items():
        if st.get("entity_type") in exclude_types or eid in closed:
            continue
        _, critical = lines(st.get("entity_type", ""))
        if projected(st) >= critical:
            roots.append(eid)
    roots.sort(key=lambda e: (-int(node_state[e].get("risk_score", 0)), -projected(node_state[e]), e))
    return roots[:limit]


def _confidence_at(confidence: dict[str, Any], entity_id: str, eta_sec: int) -> float | None:
    """The model's probability for `entity_id` at the first horizon that covers
    `eta_sec` (the last horizon beyond them). A plain float applies to any eta."""
    value = confidence.get(entity_id)
    if value is None or not isinstance(value, dict):
        return value
    if not value:
        return None
    for h in sorted(value):
        if eta_sec <= h:
            return value[h]
    return value[max(value)]


def build_cascade(root: str, node_state: dict[str, dict], out_edges: dict[str, list[dict]],
                  lines: Callable[[str], tuple[float, float]], closed: set[str], max_depth: int,
                  max_steps: int, generated_at: str, confidence: dict[str, Any] | None = None,
                  model_version: str | None = None) -> dict:
    confidence = confidence or {}
    rs = node_state[root]
    r_warn, r_crit = lines(rs.get("entity_type", ""))
    r_proj = projected(rs)
    r_cap = float(rs.get("nominal_capacity") or 1.0)
    root_eta = int(rs.get("time_to_critical_sec") or 0)
    overflow = max(r_proj - r_crit, MIN_ROOT_OVERFLOW) * r_cap
    steps: list[dict] = [{
        "entity_id": root, "predicted_band": "critical", "eta_sec": root_eta,
        "failure_probability": round(min(r_proj / r_crit, 0.99), 3), "via_edge_id": None, "depth": 0,
        "source_entity_id": None, "utilisation_before": round(float(rs.get("utilisation", 0.0)), 4),
        "utilisation_after": round(r_proj, 4), "flow_change_people": round(overflow, 1),
        "reason": f"{rs.get('display_name', root)} is at or forecast over its critical line "
                  f"({r_proj:.0%} of capacity); {overflow:,.0f} people above the line must go elsewhere.",
        "confidence": _confidence_at(confidence, root, root_eta),
    }]
    visited = {root}
    frontier: deque[tuple[str, int, float, int]] = deque([(root, 0, overflow, root_eta)])
    while frontier and len(steps) < max_steps + 1:
        node, depth, load, t = frontier.popleft()
        if depth >= max_depth or load <= 0:
            continue
        outs = [e for e in out_edges.get(node, [])
                if e["edge_type"] in FLOW_EDGE_TYPES and float(e.get("transfer_coefficient") or 0.0) > 0
                and e["dst_entity_id"] in node_state and e["dst_entity_id"] not in closed]
        total_w = sum(float(e["transfer_coefficient"]) for e in outs if e["edge_type"] in PEOPLE_EDGE_TYPES)
        node_after = next((s["utilisation_after"] for s in steps if s["entity_id"] == node), projected(node_state[node]))
        # Every outbound edge is evaluated; the overflow is split, not copied.
        for e in sorted(outs, key=lambda x: x["edge_id"]):
            dst = e["dst_entity_id"]
            if dst in visited:
                continue
            ds = node_state[dst]
            cap = float(ds.get("nominal_capacity") or 1.0)
            before = projected(ds)
            coeff = float(e["transfer_coefficient"])
            if e["edge_type"] in IMPACT_EDGE_TYPES:
                share, moved = None, None
                after = before + coeff * max(0.0, node_after - EMERGENCY_TRIGGER_UTIL) * EMERGENCY_GAIN
            else:
                share = coeff / total_w
                moved = load * share
                after = before + moved / cap
            after = min(after, PHYSICAL_MAX.get(ds.get("entity_type", ""), after))
            warn, crit = lines(ds.get("entity_type", ""))
            band = _band(after, warn, crit)
            if band is None:
                continue  # spare capacity absorbs this share: the cascade stops on this branch
            visited.add(dst)
            eta = int(t + int(e.get("travel_time_sec") or 0))
            steps.append({
                "entity_id": dst, "predicted_band": band, "eta_sec": eta,
                "failure_probability": round(min(after / crit, 0.99), 3), "via_edge_id": e["edge_id"],
                "depth": depth + 1, "source_entity_id": node,
                "utilisation_before": round(before, 4), "utilisation_after": round(after, 4),
                "flow_change_people": None if moved is None else round(moved, 1),
                "reason": (
                    f"Incident load rises because {node_state[node].get('display_name', node)} is at {node_after:.0%}: "
                    f"{before:.0%} -> {after:.0%} of capacity."
                    if moved is None else
                    f"Receives {moved:,.0f} people ({share:.0%} of the overflow) from "
                    f"{node_state[node].get('display_name', node)} via {e['edge_type'].replace('_', ' ')}: "
                    f"{before:.0%} -> {after:.0%} of capacity."
                ),
                "confidence": _confidence_at(confidence, dst, eta),
            })
            if len(steps) >= max_steps + 1:
                break
            # An emergency post passes no people on; a people node passes on
            # only what exceeds its own critical line.
            passed_on = 0.0 if moved is None else max(0.0, after - crit) * cap
            if passed_on > 0:
                frontier.append((dst, depth + 1, passed_on, eta))
    steps.sort(key=lambda s: (s["eta_sec"], s["depth"], s["entity_id"]))
    for i, s in enumerate(steps):
        s["step_index"] = i
    return {
        "root_entity_id": root, "source": "deterministic", "generated_at": generated_at,
        "total_downstream_failures": len(steps) - 1,
        "max_depth": max((s["depth"] for s in steps), default=0),
        "steps": steps, "ml_enhanced": bool(confidence),
        # Who produced `step.confidence` (`source` stays the producer of the structure, 00 §2.5).
        "confidence_source": "gnn" if confidence else None,
        "confidence_model_version": model_version if confidence else None,
    }


def build_cascades(node_state: dict[str, dict], edges: list[dict], lines: Callable[[str], tuple[float, float]],
                   cfg: dict[str, Any], generated_at: str, closed: set[str] | None = None,
                   confidence: dict[str, Any] | None = None, model_version: str | None = None) -> list[dict]:
    closed = set(closed or set())
    out_edges: dict[str, list[dict]] = {}
    for e in edges:
        out_edges.setdefault(e["src_entity_id"], []).append(e)
    roots = select_roots(node_state, lines, set(cfg.get("exclude_root_types", ["hotel"])), closed,
                         int(cfg.get("max_roots", 5)))
    return [build_cascade(r, node_state, out_edges, lines, closed, int(cfg.get("max_depth", 4)),
                          int(cfg.get("max_steps", 8)), generated_at, confidence, model_version) for r in roots]


def cascade_for(root: str, node_state: dict[str, dict], edges: list[dict],
                lines: Callable[[str], tuple[float, float]], cfg: dict[str, Any], generated_at: str,
                closed: set[str] | None = None, confidence: dict[str, Any] | None = None,
                model_version: str | None = None) -> dict:
    """The cascade from one chosen entity (on demand, or a what-if's worst point)."""
    out_edges: dict[str, list[dict]] = {}
    for e in edges:
        out_edges.setdefault(e["src_entity_id"], []).append(e)
    return build_cascade(root, node_state, out_edges, lines, set(closed or set()), int(cfg.get("max_depth", 4)),
                         int(cfg.get("max_steps", 8)), generated_at, confidence, model_version)


def ml_confidence(ml_cascades: list[dict] | None) -> dict[str, float]:
    """Per-entity failure probability from an ML cascade model's output (if any).

    Root steps (depth 0) are skipped: a cascade model reports its root's
    `failure_probability` as utilisation / critical line, which is arithmetic,
    not a model output, and must not be published as ML confidence."""
    out: dict[str, float] = {}
    for c in ml_cascades or []:
        if c.get("source") != "gnn":
            continue
        for s in c.get("steps", []):
            if s.get("depth", 0) == 0:
                continue
            p = float(s.get("failure_probability", 0.0))
            out[s["entity_id"]] = max(out.get(s["entity_id"], 0.0), p)
    return out


def horizon_probabilities(risk: dict | None) -> dict[str, dict[int, float]]:
    """`node_risk()` output -> {entity_id: {horizon_sec: probability}} for `confidence`."""
    out: dict[str, dict[int, float]] = {}
    for eid, row in ((risk or {}).get("nodes") or {}).items():
        by_h = {int(k.rsplit("_", 1)[1]): float(v) for k, v in row.items() if k.startswith("p_fail_") and v is not None}
        if by_h:
            out[eid] = by_h
    return out
