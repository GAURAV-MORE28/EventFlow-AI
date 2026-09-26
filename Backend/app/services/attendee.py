"""Attendee-facing services (01_BACKEND_CONTRACT.md §3.12).

The design point: **both routes are always returned**, because the whole argument
of the attendee view is that the shortest route is the worse route. Filtering the
bad one out server-side would delete the demo.
"""
from __future__ import annotations

import heapq
import logging
from typing import Any

from ..errors import ApiError
from ..ml_reference.common import band_from_score, clamp, stable_unit
from ..simtime import shift

log = logging.getLogger("eventflow.attendee")

# Edge types an attendee can actually travel along, and how it reads in the UI.
TRAVERSABLE = {
    "feeds": "transit",
    "last_mile_to": "walk",
    "adjacent_to": "walk",
    "serves": "shuttle",
}


def _graph(store: Any) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for e in store.edges:
        if e["edge_type"] in TRAVERSABLE:
            out.setdefault(e["src_entity_id"], []).append(e)
    return out


def _search(store: Any, origin: str, destination: str, weight) -> list[dict] | None:
    """Dijkstra over the traversable graph with a pluggable edge weight."""
    graph = _graph(store)
    dist: dict[str, float] = {origin: 0.0}
    prev: dict[str, tuple[str, dict]] = {}
    queue: list[tuple[float, str]] = [(0.0, origin)]
    seen: set[str] = set()

    while queue:
        cost, node = heapq.heappop(queue)
        if node in seen:
            continue
        seen.add(node)
        if node == destination:
            break
        for edge in graph.get(node, []):
            dst = edge["dst_entity_id"]
            new_cost = cost + weight(edge, dst)
            if new_cost < dist.get(dst, float("inf")):
                dist[dst] = new_cost
                prev[dst] = (node, edge)
                heapq.heappush(queue, (new_cost, dst))

    if destination not in prev and destination != origin:
        return None

    legs: list[dict] = []
    cursor = destination
    while cursor != origin:
        src, edge = prev[cursor]
        legs.append(
            {
                "from_entity_id": src,
                "to_entity_id": cursor,
                "mode": TRAVERSABLE[edge["edge_type"]],
                "duration_sec": int(edge.get("travel_time_sec", 0)) or 120,
            }
        )
        cursor = src
    legs.reverse()
    return legs


def _route(store: Any, legs: list[dict]) -> dict:
    total = sum(l["duration_sec"] for l in legs)
    # A route is as crowded as its worst leg — averaging hides the pinch point.
    worst = 0
    for leg in legs:
        state = store.entity_states.get(leg["to_entity_id"])
        if state:
            worst = max(worst, int(state["risk_score"]))
    return {
        "legs": legs,
        "total_duration_sec": total,
        "predicted_crowding_band": band_from_score(worst),
    }


def build_journey(engine: Any, request: dict) -> dict:
    store = engine.store
    origin = request["origin_entity_id"]
    destination = request["destination_entity_id"]

    for eid in (origin, destination):
        if eid not in store.nodes:
            raise ApiError("ENTITY_NOT_FOUND", f"No entity with id '{eid}'.", {"entity_id": eid})

    segment = next(
        (s for s in store.segments if s["segment_id"] == request["segment_id"]), store.segments[0]
    )
    accessible_only = bool(segment["accessibility_constrained"])

    def time_weight(edge: dict, dst: str) -> float:
        base = float(edge.get("travel_time_sec", 0)) or 120.0
        if accessible_only and store.nodes[dst]["entity_type"] == "gate":
            base *= 1.15   # step-free routing detour
        return base

    def risk_weight(edge: dict, dst: str) -> float:
        base = time_weight(edge, dst)
        state = store.entity_states.get(dst, {})
        util = float(state.get("utilisation", 0.0))
        # Congestion cost grows superlinearly — the same shape the solver uses.
        return base * (1.0 + 3.0 * (util ** 2.5))

    shortest_legs = _search(store, origin, destination, time_weight)
    recommended_legs = _search(store, origin, destination, risk_weight)

    if not shortest_legs:
        raise ApiError(
            "ENTITY_NOT_FOUND",
            f"No route from '{origin}' to '{destination}'.",
            {"origin_entity_id": origin, "destination_entity_id": destination},
        )
    recommended_legs = recommended_legs or shortest_legs

    shortest = _route(store, shortest_legs)
    recommended = _route(store, recommended_legs)

    # Journey risk follows the route we actually recommend.
    scores = [
        int(store.entity_states.get(l["to_entity_id"], {}).get("risk_score", 0))
        for l in recommended_legs
    ]
    journey_score = int(round(clamp(max(scores) if scores else 0, 0, 100)))

    extra = recommended["total_duration_sec"] - shortest["total_duration_sec"]
    if recommended["predicted_crowding_band"] != shortest["predicted_crowding_band"]:
        advice = (
            f"The faster route runs through {shortest['predicted_crowding_band']} crowding. "
            f"The recommended route adds {max(extra, 0) // 60} minutes and avoids it."
        )
    else:
        advice = "Both routes carry similar crowding right now; the faster one is fine."

    return {
        "journey_risk_score": journey_score,
        "journey_risk_band": band_from_score(journey_score),
        "recommended_route": recommended,
        "shortest_route": shortest,
        "advice": advice,
    }


def issue_nudges(engine: Any, intervention: dict) -> list[dict]:
    """Turn an approved intervention into attendee-facing nudges with honest trade-offs."""
    store = engine.store
    # Phase 1C: a "switch to X" nudge only makes sense when the action actually
    # moves people to X. Before, the last target was used, so approving
    # notify_only told attendees to "Switch to" the congested station itself.
    destinations = [
        e.get("destination_entity_id") for e in intervention.get("action_effects") or []
        if e.get("destination_entity_id") in store.nodes
    ]
    targets = [t for t in intervention["target_entity_ids"] if t in store.nodes]
    if not destinations or not targets:
        return []
    destination = destinations[0]
    node = store.nodes[destination]

    credit = int(intervention["estimated_cost_paise"] / max(len(targets), 1) / 3)
    extra_travel = int(intervention["estimated_delay_sec"])

    issued: list[dict] = []
    for i, segment in enumerate(store.segments[:3]):
        attendee_id = f"att_demo_{i + 1}"
        nid = "ndg_" + f"{int(stable_unit(intervention['intervention_id'], attendee_id) * 0xFFFF):04x}"
        nudge = {
            "nudge_id": nid,
            "attendee_id": attendee_id,
            "intervention_id": intervention["intervention_id"],
            "headline": f"Switch to {node['display_name']}",
            "body": (
                f"{max(extra_travel // 60, 1)} minutes further, "
                f"Rs {credit // 100} credit, and priority shuttle access."
            ),
            "tradeoff": {
                "extra_travel_sec": extra_travel,
                "credit_paise": credit,
                "perk": "priority_shuttle",
            },
            "target_entity_id": destination,
            "status": "pending",
            "issued_at": store.sim_time,
            "expires_at": shift(store.sim_time, 900),
        }
        store.nudges[nid] = nudge
        issued.append(nudge)
    return issued
