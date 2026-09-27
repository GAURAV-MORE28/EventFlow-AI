"""Attendee-facing services (01_BACKEND_CONTRACT.md §3.12).

Journey planning runs Dijkstra over the venue graph with *time-dependent*
weights: each leg is costed with the crowding and queue delay the look-ahead
projection expects at the moment the attendee would reach that node. Every
answer comes from the simulation, never from a fixed label:

* recommended / fastest / least-crowded / a node-disjoint alternative route
* departure options (now, +15, +30, +45 min) with the lowest-pressure pick
* the return journey, planned for after the attendee's event ends
* nudges only reach attendees whose planned route passes an intervention's
  source, and accepting one changes that attendee's plan and the city's
  compliance estimate.
"""
from __future__ import annotations

import heapq
import logging
from typing import Any, Callable

from ..config import get_config
from ..errors import ApiError
from ..ml_reference.common import band_from_score, clamp, stable_unit
from ..simtime import parse, shift
from .projection import PROJECTIONS, ProjectionService

log = logging.getLogger("eventflow.attendee")

# Edge types an attendee can travel along, and how the leg reads in the UI.
TRAVERSABLE = {
    "feeds": "transit",
    "last_mile_to": "walk",
    "adjacent_to": "walk",
    "serves": "shuttle",
    "connects_to": "walk",      # generated worlds: a station / hotel / car park onto its road
}
RAIL_SUBTYPES = {"metro_station", "rail_station", "metro_line"}
BUS_SUBTYPES = {"bus_hub", "bus_station", "bus_stop_cluster", "bus_stop", "shuttle_hub"}
CROWD_WEIGHT = {"fastest": 0.0, "balanced": 1.0, "least_crowded": 3.0}
# Travel-mode preference: a soft cost on the nodes of other modes, so the
# preferred mode wins unless it is closed or unreasonably slow.
MODE_PENALTY, MODE_BONUS = 5.0, 0.7


def mode_class(node: dict | None) -> str | None:
    """Travel mode an entity belongs to, from its type/subtype (never its id)."""
    if not node:
        return None
    sub = node.get("subtype")
    if node.get("entity_type") == "transport_route" or sub in RAIL_SUBTYPES:
        return "metro"
    if sub in BUS_SUBTYPES:
        return "bus"
    if node.get("entity_type") == "parking":
        return "car"
    return None


def preference_penalty(store: Any, preference: str | None) -> dict[str, float]:
    if not preference or preference == "any":
        return {}
    out = {}
    for eid, node in store.nodes.items():
        cls = mode_class(node)
        if cls is None:
            continue
        out[eid] = MODE_BONUS if cls == preference else MODE_PENALTY
    return out


def preference_met(store: Any, route: dict, preference: str | None) -> bool | None:
    if not preference or preference == "any":
        return None
    used = {mode_class(store.nodes.get(l["to_entity_id"])) for l in route["legs"]} | \
        {mode_class(store.nodes.get(l["from_entity_id"])) for l in route["legs"]}
    used.discard(None)
    return not used if preference == "walk" else preference in used


def access_links(store: Any, origin: str, destination: str, preference: str | None) -> list[tuple[str, str, dict]]:
    """Door-to-door links the transit graph does not model: driving from the
    origin to a car park, or walking straight to the venue's gates. Travel
    times come from the geo provider (synthetic unless OSRM/Google is set)."""
    from ..providers.geo import get_geo_provider

    if preference not in ("car", "walk"):
        return []
    o = store.nodes.get(origin)
    if o is None:
        return []
    geo = get_geo_provider()
    if preference == "car":
        targets, mode = [e for e, n in store.nodes.items() if n["entity_type"] == "parking"], "drive"
    else:
        gates = [e["src_entity_id"] for e in store.edges
                 if e["dst_entity_id"] == destination and store.nodes[e["src_entity_id"]]["entity_type"] == "gate"]
        targets, mode = gates or [destination], "walk"
    out = []
    for t in targets:
        n = store.nodes[t]
        r = geo.travel((o["lat"], o["lon"]), (n["lat"], n["lon"]), mode)
        out.append((origin, t, {
            "edge_id": f"{origin}__{t}__{mode}", "src_entity_id": origin, "dst_entity_id": t,
            "edge_type": "last_mile_to", "travel_time_sec": int(r["duration_sec"]), "geo_source": r["source"],
        }))
    return out


# Nodes whose "utilisation" is not crowding a traveller moves through.
NOT_CROWD = {"hotel"}


def _graph(store: Any, closed: set[str]) -> dict[str, list[tuple[str, dict, bool]]]:
    """Adjacency over traversable edges, both directions (egress walks them backwards).

    A closed entity is removed; anyone who would have reached it walks on to its
    `substitutes_for` alternative instead (the same rule the simulator applies),
    so a station outage re-routes a trip rather than stranding it.
    """
    out: dict[str, list[tuple[str, dict, bool]]] = {}
    for e in store.edges:
        if e["edge_type"] not in TRAVERSABLE:
            continue
        a, b = e["src_entity_id"], e["dst_entity_id"]
        if a in closed or b in closed:
            continue
        out.setdefault(a, []).append((b, e, False))
        out.setdefault(b, []).append((a, e, True))
    for x in closed:
        subs = [e for e in store.edges if e["src_entity_id"] == x and e["edge_type"] == "substitutes_for"
                and e["dst_entity_id"] not in closed]
        if not subs:
            continue
        preds = [e for e in store.edges if e["dst_entity_id"] == x and e["edge_type"] in TRAVERSABLE
                 and e["src_entity_id"] not in closed]
        for p in preds:
            for sub in subs:
                walk = {
                    "edge_id": f"{p['src_entity_id']}__{sub['dst_entity_id']}__detour",
                    "src_entity_id": p["src_entity_id"], "dst_entity_id": sub["dst_entity_id"],
                    "edge_type": "last_mile_to",
                    "travel_time_sec": int(p.get("travel_time_sec", 0)) + int(sub.get("travel_time_sec", 0)),
                }
                out.setdefault(p["src_entity_id"], []).append((sub["dst_entity_id"], walk, False))
                out.setdefault(sub["dst_entity_id"], []).append((p["src_entity_id"], walk, True))
    return out


def _band(engine: Any, util: float) -> str:
    risk = engine.registry.risk
    if hasattr(risk, "utilisation_floor"):
        return band_from_score(risk.utilisation_floor(util), engine.config.raw["thresholds"]["risk_bands"])
    return band_from_score(clamp(util * 100.0, 0, 100), engine.config.raw["thresholds"]["risk_bands"])


class Planner:
    """Time-dependent routing against one projection."""

    def __init__(self, engine: Any, projection: dict[str, Any], avoid: set[str] | None = None) -> None:
        self.engine = engine
        self.store = engine.store
        self.projection = projection
        with engine.world_lock:
            closed = engine.generator.closed_entities() if hasattr(engine.generator, "closed_entities") else set()
        self.closed = closed
        self.graph = _graph(self.store, closed)
        self.avoid = set(avoid or set())
        acfg = engine.config.raw.get("attendee", {})
        self.alpha = float(acfg.get("congestion_alpha", 0.15))
        self.beta = float(acfg.get("congestion_beta", 4.0))

    def slowdown(self, util: float) -> float:
        """BPR travel-time multiplier 1 + alpha * u^beta (u capped at the physical max)."""
        u = min(max(util, 0.0), 1.8)
        return 1.0 + self.alpha * u ** self.beta

    def load(self, eid: str, offset_sec: float) -> tuple[float, float]:
        sample = ProjectionService.at(self.projection, offset_sec)
        return float(sample["util"].get(eid, 0.0)), float(sample["delay"].get(eid, 0.0))

    def search(self, origin: str, destination: str, depart_offset: float, crowd_w: float,
               node_penalty: dict[str, float] | None = None) -> list[dict] | None:
        node_penalty = node_penalty or {}
        best: dict[str, float] = {origin: 0.0}
        clock: dict[str, float] = {origin: 0.0}
        prev: dict[str, tuple[str, dict, bool, float, float]] = {}
        heap = [(0.0, origin)]
        done: set[str] = set()
        types = {e: n["entity_type"] for e, n in self.store.nodes.items()}
        inbound = types.get(destination) in ("venue", "zone")
        while heap:
            cost, node = heapq.heappop(heap)
            if node in done:
                continue
            done.add(node)
            if node == destination:
                break
            for nxt, edge, reverse in self.graph.get(node, []):
                if nxt in done:
                    continue
                if nxt in self.avoid and nxt != destination:
                    continue
                base = float(edge.get("travel_time_sec", 0)) or 120.0
                t = clock[node] + base
                util, delay = self.load(nxt, depart_offset + t)
                if types.get(nxt) in NOT_CROWD or nxt == destination:
                    util_c = 0.0
                else:
                    util_c = util
                # Station queues hold travellers either way; a gate's scan queue
                # only matters on the way *into* a venue. The edge's stored
                # direction says nothing about the trip's direction.
                if types.get(nxt) == "gate":
                    if not inbound:
                        delay = 0.0
                elif types.get(nxt) != "transport_node":
                    delay = 0.0
                # Crowded roads, zones and trains are slower to move through
                # (BPR volume-delay), at the load projected for the moment the
                # traveller gets there; queues add their own wait on top.
                seg = base * self.slowdown(util_c) + delay
                weight = seg * (1.0 + crowd_w * 3.0 * max(0.0, util_c) ** 2.5) * node_penalty.get(nxt, 1.0)
                if cost + weight < best.get(nxt, float("inf")):
                    best[nxt] = cost + weight
                    clock[nxt] = clock[node] + seg
                    prev[nxt] = (node, edge, reverse, seg, delay)
                    heapq.heappush(heap, (cost + weight, nxt))
        if destination not in prev:
            return None
        legs = []
        cur = destination
        while cur != origin:
            src, edge, reverse, seg, delay = prev[cur]
            legs.append({"from": src, "to": cur, "edge": edge, "reverse": reverse, "seconds": seg, "delay": delay})
            cur = src
        legs.reverse()
        return legs

    def _mode(self, a: str, b: str, types: dict[str, str]) -> str:
        """How the traveller actually moves on a leg, from what it connects."""
        ends = {types.get(a), types.get(b)}
        if "transport_route" in ends:
            return "transit"
        if "parking" in ends:
            # Drive to/from the car park; the car park <-> gate stretch is on foot.
            return "walk" if "gate" in ends else "drive"
        if any(mode_class(self.store.nodes.get(x)) == "bus" for x in (a, b)) and "gate" not in ends:
            return "shuttle"
        return "walk"

    def route(self, legs: list[dict], depart_offset: float, label: str, destination: str) -> dict:
        types = {e: n["entity_type"] for e, n in self.store.nodes.items()}
        out_legs, t, peak, delay_total = [], 0.0, 0.0, 0.0
        for leg in legs:
            t += leg["seconds"]
            delay_total += leg["delay"]
            if types.get(leg["to"]) not in NOT_CROWD and leg["to"] != destination:
                util, _ = self.load(leg["to"], depart_offset + t)
                peak = max(peak, util)
            mode = self._mode(leg["from"], leg["to"], types)
            out_legs.append({
                "from_entity_id": leg["from"], "to_entity_id": leg["to"], "mode": mode,
                "duration_sec": int(round(leg["seconds"])),
            })
        total = int(round(sum(l["duration_sec"] for l in out_legs)))
        return {
            "legs": out_legs,
            "total_duration_sec": total,
            "predicted_crowding_band": _band(self.engine, peak),
            "label": label,
            "peak_utilisation": round(peak, 4),
            "congestion_delay_sec": int(round(delay_total)),
            "depart_at": shift(self.projection["sim_time"], depart_offset),
        }


def _event_at(engine: Any, venue: str) -> dict | None:
    evs = [e for e in engine.events.events.values() if e["venue_entity_id"] == venue and e["status"] != "cancelled"]
    if not evs:
        return None
    now = parse(engine.store.sim_time)
    upcoming = [e for e in evs if parse(e["end_time"]) > now]
    return min(upcoming or evs, key=lambda e: e["start_time"])


def _resolve_origin(engine: Any, origin: str, property_id: str | None) -> tuple[str, dict | None]:
    store = engine.store
    prop = None
    pid = property_id or (origin if any(p["property_id"] == origin for p in store.properties) else None)
    if pid:
        prop = next((p for p in store.properties if p["property_id"] == pid), None)
        if prop is None:
            raise ApiError("PROPERTY_NOT_FOUND", f"No property with id '{pid}'.", {"property_id": pid})
        origin = prop["cluster_entity_id"]
    if origin not in store.nodes:
        raise ApiError("ENTITY_NOT_FOUND", f"No entity with id '{origin}'.", {"entity_id": origin})
    return origin, prop


def build_journey(engine: Any, request: dict) -> dict:
    store = engine.store
    origin, prop = _resolve_origin(engine, request["origin_entity_id"], request.get("hotel_property_id"))
    destination = request["destination_entity_id"]
    if destination not in store.nodes:
        raise ApiError("ENTITY_NOT_FOUND", f"No entity with id '{destination}'.", {"entity_id": destination})
    if destination == origin:
        raise ApiError("INVALID_REQUEST", "Origin and destination are the same place.",
                       {"origin_entity_id": origin, "destination_entity_id": destination})
    segment = next((s for s in store.segments if s["segment_id"] == request["segment_id"]), store.segments[0])
    priority = request.get("priority") or "balanced"
    if segment["accessibility_constrained"] and priority == "fastest":
        priority = "balanced"  # step-free routing never optimises purely for speed
    attendee = store.attendees.get(request["attendee_id"], {})
    avoid = set(attendee.get("avoid", set())) - {origin, destination}

    projection = PROJECTIONS.get(engine)
    planner = Planner(engine, projection, avoid)
    now = parse(store.sim_time)
    base_offset = 0.0
    if request.get("planned_departure"):
        base_offset = max(0.0, (parse(request["planned_departure"]) - now).total_seconds())
    base_offset = max(base_offset, float(attendee.get("depart_offset_sec", 0.0)))

    preference = request.get("transport_preference")
    mode_pen = preference_penalty(store, preference)
    for a, b, edge in access_links(store, origin, destination, preference):
        planner.graph.setdefault(a, []).append((b, edge, False))
        planner.graph.setdefault(b, []).append((a, edge, True))

    def plan(offset: float, crowd_w: float, label: str, penalty: dict | None = None) -> dict | None:
        penalty = {**mode_pen, **{k: v * mode_pen.get(k, 1.0) for k, v in (penalty or {}).items()}}
        legs = planner.search(origin, destination, offset, crowd_w, penalty)
        if legs is None and planner.avoid:
            planner.avoid = set()
            legs = planner.search(origin, destination, offset, crowd_w, penalty)
            planner.avoid = avoid
        return None if legs is None else planner.route(legs, offset, label, destination)

    fastest = plan(base_offset, CROWD_WEIGHT["fastest"], "Fastest")
    if fastest is None:
        raise ApiError("ENTITY_NOT_FOUND", f"No route from '{origin}' to '{destination}'.",
                       {"origin_entity_id": origin, "destination_entity_id": destination})
    recommended = plan(base_offset, CROWD_WEIGHT[priority], {
        "fastest": "Fastest", "balanced": "Recommended", "least_crowded": "Least crowded",
    }[priority]) or fastest
    quiet = plan(base_offset, CROWD_WEIGHT["least_crowded"], "Least crowded")
    rec_nodes = {l["to_entity_id"] for l in recommended["legs"][:-1]}
    diverse = plan(base_offset, CROWD_WEIGHT["balanced"], "Alternative", {n: 3.0 for n in rec_nodes})

    seen = {tuple(l["to_entity_id"] for l in recommended["legs"])}
    alternatives = []
    for r in (fastest, quiet, diverse):
        if r is None:
            continue
        key = tuple(l["to_entity_id"] for l in r["legs"])
        if key in seen:
            continue
        seen.add(key)
        alternatives.append(r)

    # Walking time from the chosen property to its transport node replaces the
    # cluster-level estimate on the first leg.
    if prop is not None:
        for r in [recommended, fastest] + alternatives:
            if r["legs"] and r["legs"][0]["from_entity_id"] == prop["cluster_entity_id"]:
                diff = int(prop["walk_to_transport_sec"]) - r["legs"][0]["duration_sec"]
                r["legs"][0]["duration_sec"] = int(prop["walk_to_transport_sec"])
                r["total_duration_sec"] = max(0, r["total_duration_sec"] + diff)

    # --- departure options (off-peak) ---------------------------------------------
    event = _event_at(engine, destination)
    cfg = get_config().raw
    buffer = int(cfg.get("attendee", {}).get("arrival_buffer_sec", 600))
    offsets = [int(o) for o in cfg.get("attendee", {}).get("departure_offsets_sec", [0, 900, 1800, 2700])]
    options = []
    for off in offsets:
        r = plan(base_offset + off, CROWD_WEIGHT[priority], "option")
        if r is None:
            continue
        depart = shift(store.sim_time, base_offset + off)
        arrive = shift(depart, r["total_duration_sec"])
        meets = None
        if event and parse(event["start_time"]) > now:
            meets = parse(arrive) <= parse(shift(event["start_time"], -buffer))
        options.append({
            "offset_sec": int(base_offset + off), "depart_at": depart, "arrive_at": arrive,
            "travel_time_sec": r["total_duration_sec"], "peak_utilisation": r["peak_utilisation"],
            "crowding_band": r["predicted_crowding_band"], "meets_event_start": meets, "recommended": False,
        })
    departure_advice = None
    rec_offset = None
    if options:
        eligible = [o for o in options if o["meets_event_start"] is not False] or options
        best_travel = min(o["travel_time_sec"] for o in eligible)
        # Lowest crowding, among options whose trip is not much slower than the quickest.
        pick = min(
            (o for o in eligible if o["travel_time_sec"] <= best_travel * 1.25 + 120),
            key=lambda o: (round(o["peak_utilisation"], 2), o["travel_time_sec"], o["offset_sec"]),
        )
        # Waiting has a cost: only advise a later departure when it lowers peak
        # crowding meaningfully; otherwise the earliest option wins.
        earliest = eligible[0]
        min_gain = float(cfg.get("attendee", {}).get("min_crowding_gain", 0.05))
        if pick is not earliest and earliest["peak_utilisation"] - pick["peak_utilisation"] < min_gain:
            pick = earliest
        pick["recommended"] = True
        rec_offset = pick["offset_sec"]
        first = options[0]
        if pick is first:
            departure_advice = (
                f"Leave now: waiting does not meaningfully reduce crowding "
                f"(peak {int(round(first['peak_utilisation'] * 100))}% on your route)."
            )
        else:
            departure_advice = (
                f"Leave in {round((pick['offset_sec'] - base_offset) / 60)} minutes: peak crowding on your route drops "
                f"from {int(round(first['peak_utilisation'] * 100))}% to {int(round(pick['peak_utilisation'] * 100))}%"
            )
            if pick["meets_event_start"]:
                departure_advice += f" and you still arrive before {event['name']} starts."
            else:
                departure_advice += "."
        if all(o["meets_event_start"] is False for o in options if o["meets_event_start"] is not None) and event:
            departure_advice = f"Leave now: every later option arrives after {event['name']} starts."

    # --- return journey ---------------------------------------------------------------
    return_route = None
    if request.get("include_return", True):
        ret_offset = 0.0
        label = "Return"
        if event:
            end_offset = (parse(event["end_time"]) - now).total_seconds()
            ret_offset = max(0.0, end_offset + 600)
            label = f"Return after {event['name']}"
        legs = planner.search(destination, origin, ret_offset, CROWD_WEIGHT[priority])
        if legs is not None:
            return_route = planner.route(legs, ret_offset, label, origin)
            if ret_offset > projection.get("horizon_sec", 0):
                return_route["label"] = label + " (beyond projection horizon: current conditions)"

    worst = max(recommended["peak_utilisation"], 0.0)
    journey_score = int(round(engine.registry.risk.utilisation_floor(worst))) if hasattr(engine.registry.risk, "utilisation_floor") else int(round(clamp(worst * 100, 0, 100)))

    extra = recommended["total_duration_sec"] - fastest["total_duration_sec"]
    if recommended["legs"] == fastest["legs"]:
        advice = (
            f"The fastest route is also the best balance right now "
            f"(peak {int(round(fastest['peak_utilisation'] * 100))}% crowding)."
        )
    else:
        advice = (
            f"The fastest route peaks at {int(round(fastest['peak_utilisation'] * 100))}% crowding. "
            f"The recommended route adds {max(extra, 0) // 60} minutes and peaks at "
            f"{int(round(recommended['peak_utilisation'] * 100))}%."
        )

    route_entities = {l["to_entity_id"] for r in [recommended] + ([return_route] if return_route else []) for l in r["legs"]}
    route_entities.add(origin)
    store.attendees[request["attendee_id"]] = {
        **attendee,
        "attendee_id": request["attendee_id"],
        "segment_id": segment["segment_id"],
        "origin": origin,
        "destination": destination,
        "hotel_cluster": prop["cluster_entity_id"] if prop else (origin if store.nodes[origin]["entity_type"] == "hotel" else attendee.get("hotel_cluster")),
        "property_id": prop["property_id"] if prop else attendee.get("property_id"),
        "route_entities": sorted(route_entities),
        "avoid": set(attendee.get("avoid", set())),
        "updated_at": store.sim_time,
    }

    event_view = None
    if event:
        event_view = {k: event[k] for k in ("event_id", "name", "start_time", "end_time", "venue_entity_id")}
    return {
        "journey_risk_score": journey_score,
        "journey_risk_band": band_from_score(journey_score, engine.config.raw["thresholds"]["risk_bands"]),
        "recommended_route": recommended,
        "shortest_route": fastest,
        "advice": advice,
        "priority": priority,
        "alternatives": alternatives,
        "departure_options": options,
        "recommended_departure_offset_sec": rec_offset,
        "departure_advice": departure_advice,
        "return_route": return_route,
        "event": event_view,
        "avoided_entity_ids": sorted(avoid),
        "transport_preference": preference,
        "preference_met": preference_met(store, recommended, preference),
    }


# --- nudges ------------------------------------------------------------------------------
def _sources_and_destination(item: dict) -> tuple[list[str], str | None]:
    action = item.get("_action") or {}
    targets = item["target_entity_ids"]
    t = item["intervention_type"]
    if t == "gate_redistribution":
        return list(action.get("sources") or targets[:-1]), action.get("destination") or targets[-1]
    if t == "stagger_entry":
        return list(action.get("gates") or targets), None
    if t in ("reroute_transport", "parking_redistribution", "zone_incentive", "accommodation_rebalance"):
        return [action.get("source") or targets[0]], action.get("destination") or (targets[1] if len(targets) > 1 else None)
    return [], None


def _message(engine: Any, item: dict, src: str, dst: str | None) -> tuple[str, str, int, str | None]:
    names = {e: n["display_name"] for e, n in engine.store.nodes.items()}
    t = item["intervention_type"]
    extra = int(item.get("estimated_delay_sec", 0))
    if t == "stagger_entry":
        delay = int(float((item.get("_action") or {}).get("delay_min", 12)))
        return (f"Arrive {delay} minutes later",
                f"{names.get(src, src)} is crowded. Arriving {delay} minutes later avoids the peak queue.",
                delay * 60, "fast_lane_entry")
    if t == "accommodation_rebalance":
        return (f"A room is available at {names.get(dst, dst)}",
                f"{names.get(src, src)} is fully booked. Transfer to {names.get(dst, dst)} with a free transfer and credit.",
                extra, "free_transfer")
    if t == "reroute_transport":
        return (f"Use {names.get(dst, dst)} instead of {names.get(src, src)}",
                f"{names.get(src, src)} is congested. {names.get(dst, dst)} is quieter.", extra, "priority_shuttle")
    if t == "gate_redistribution":
        return (f"Enter through {names.get(dst, dst)}",
                f"{names.get(src, src)} has a long queue. {names.get(dst, dst)} is moving faster.", extra, "fast_lane_entry")
    if t == "parking_redistribution":
        return (f"Park at {names.get(dst, dst)}",
                f"{names.get(src, src)} is nearly full. {names.get(dst, dst)} has space.", extra, "free_transfer")
    return (f"Head to {names.get(dst, dst)}",
            f"{names.get(src, src)} is crowded. {names.get(dst, dst)} has more room.", extra, None)


def issue_nudges(engine: Any, item: dict) -> list[dict]:
    """Nudge only the attendees whose plan touches the intervention's source."""
    store = engine.store
    sources, dst = _sources_and_destination(item)
    if not sources:
        return []  # operational actions (shuttles, corridors, notifications) need nothing from attendees
    credit = int(clamp(item.get("estimated_cost_paise", 0) / 40, 5_000, 50_000))
    issued: list[dict] = []
    for attendee_id, a in sorted(store.attendees.items()):
        touched = set(a.get("route_entities", [])) | ({a.get("hotel_cluster")} if a.get("hotel_cluster") else set())
        hit = next((s for s in sources if s in touched), None)
        if hit is None:
            continue
        headline, body, extra, perk = _message(engine, item, hit, dst)
        nid = "ndg_" + f"{int(stable_unit(item['intervention_id'], attendee_id) * 0xFFFFF):05x}"
        nudge = {
            "nudge_id": nid,
            "attendee_id": attendee_id,
            "intervention_id": item["intervention_id"],
            "segment_id": a.get("segment_id"),
            "headline": headline,
            "body": body + f" Rs {credit // 100} credit.",
            "tradeoff": {"extra_travel_sec": int(extra), "credit_paise": credit, "perk": perk},
            "target_entity_id": dst or hit,
            "status": "pending",
            "issued_at": store.sim_time,
            "expires_at": shift(store.sim_time, 900),
            "_source_entity_id": hit,
            "_type": item["intervention_type"],
        }
        store.nudges[nid] = nudge
        issued.append(nudge)
    return issued


def apply_nudge_response(engine: Any, nudge: dict, accepted: bool) -> dict:
    """An accepted nudge changes this attendee's plan; every answer (accept or
    decline) updates the compliance estimate the simulator applies to active
    interventions. Returns the concrete plan change so the client can re-plan."""
    store = engine.store
    a = store.attendees.get(nudge["attendee_id"])
    change: dict = {}
    if accepted and a is not None:
        src = nudge.get("_source_entity_id")
        if nudge.get("_type") == "stagger_entry":
            a["depart_offset_sec"] = max(float(a.get("depart_offset_sec", 0.0)), float(nudge["tradeoff"]["extra_travel_sec"]))
            change["depart_offset_sec"] = int(a["depart_offset_sec"])
        elif nudge.get("_type") == "accommodation_rebalance":
            dst = nudge.get("target_entity_id")
            a["hotel_cluster"] = dst
            # The concrete room: the best-scoring property with space in the
            # destination district, for this attendee's segment.
            from .accommodation import recommend

            options = recommend(engine, destination_entity_id=a.get("destination"), segment_id=a.get("segment_id"),
                                limit=20)["options"]
            pick = next((o["property"] for o in options if o["property"]["cluster_entity_id"] == dst), None)
            if pick:
                a["property_id"] = pick["property_id"]
                change["new_origin_property_id"] = pick["property_id"]
                change["new_origin_name"] = pick["name"]
        elif src:
            a.setdefault("avoid", set()).add(src)
            change["avoid_entity_id"] = src
    engine.record_compliance(accepted, segment_id=a.get("segment_id"))
    change["compliance"] = engine.current_compliance()
    return change


def public_nudge(n: dict) -> dict:
    return {k: v for k, v in n.items() if not k.startswith("_") and k != "segment_id"}
