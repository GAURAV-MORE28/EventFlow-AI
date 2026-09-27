"""Seeded random maps for HX-Cascade training (03 §8.1 `randomise_topology`, §8.4).

Starts from a base map (the synthetic city) and changes its *structure* as well
as its numbers, so a model learns propagation mechanics rather than one map:

  * removes and adds gates, roads and crowd zones (new ones are wired the way
    the base map wires its own: gates fed by access nodes, feeding a venue,
    spilling onto a road and serving a zone; roads joined to the road network,
    some evacuating to an emergency post; zones reached last-mile from a
    station and adjacent to another zone)
  * rescales capacities per type and per entity, transfer coefficients and
    travel times
  * moves event start times, scales attendance, sometimes drops a secondary event

Hotels are left as they are (the hotel catalogue must sum to each cluster's
capacity). Every map is validated (`problems()`); an invalid draw is retried
with the next sub-seed. Pure dicts in and out — no simulator, no backend import;
the dataset generator additionally checks that the simulator can run the map.
"""
from __future__ import annotations

import copy
import random
from datetime import datetime, timedelta, timezone
from typing import Any

from ..manifest import topology_hash

ACCESS_TYPES = ("transport_node", "parking")
RESCALED_TYPES = ("gate", "road", "zone", "transport_node", "transport_route", "parking",
                  "emergency_facility", "venue")

DEFAULTS = {
    "remove_gates": (0, 2), "add_gates": (0, 2),
    "remove_roads": (0, 3), "add_roads": (0, 3),
    "remove_zones": (0, 2), "add_zones": (0, 2),
    "type_scale": (0.8, 1.25), "entity_sigma": 0.15,
    "coefficient_scale": (0.75, 1.3), "travel_scale": (0.8, 1.25),
    "event_shift_min": 75, "attendance_scale": (0.7, 1.3), "drop_secondary_event_p": 0.2,
    "min_gates_per_venue": 3, "min_roads": 6, "min_zones": 4,
}


def _edge(src: str, dst: str, edge_type: str, coeff: float, travel: int, subst: float = 0.0) -> dict:
    return {"edge_id": f"{src}__{dst}__{edge_type}", "src_entity_id": src, "dst_entity_id": dst,
            "edge_type": edge_type, "transfer_coefficient": round(coeff, 4), "travel_time_sec": int(travel),
            "substitutability": subst}


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class _Map:
    def __init__(self, topology: dict, events: list[dict]):
        self.nodes: dict[str, dict] = {n["entity_id"]: copy.deepcopy(n) for n in topology["nodes"]}
        self.edges: list[dict] = [copy.deepcopy(e) for e in topology["edges"]]
        self.segments = copy.deepcopy(topology["segments"])
        self.events = [copy.deepcopy(e) for e in events]
        self.ops: list[str] = []

    def of_type(self, t: str) -> list[str]:
        return sorted(e for e, n in self.nodes.items() if n["entity_type"] == t)

    def type(self, eid: str) -> str:
        return self.nodes[eid]["entity_type"]

    def event_venues(self) -> set[str]:
        return {e["venue_entity_id"] for e in self.events}

    def gates_of(self, venue: str) -> list[str]:
        return sorted({e["src_entity_id"] for e in self.edges
                       if e["dst_entity_id"] == venue and e["edge_type"] == "feeds"
                       and self.type(e["src_entity_id"]) == "gate"})

    def remove(self, eid: str) -> None:
        del self.nodes[eid]
        self.edges = [e for e in self.edges if eid not in (e["src_entity_id"], e["dst_entity_id"])]

    def add_node(self, eid: str, etype: str, name: str, near: str, capacity: float, rng: random.Random,
                 parent: str | None = None) -> None:
        ref = self.nodes[near]
        self.nodes[eid] = {
            "entity_id": eid, "entity_type": etype, "display_name": name,
            "lat": round(ref["lat"] + rng.uniform(-0.002, 0.002), 6),
            "lon": round(ref["lon"] + rng.uniform(-0.002, 0.002), 6),
            "nominal_capacity": float(round(capacity)), "parent_id": parent, "meta": {"generated": True},
        }

    def add_edge(self, edge: dict) -> None:
        if all(e["edge_id"] != edge["edge_id"] for e in self.edges):
            self.edges.append(edge)

    def to_topology(self, bounds_pad: float = 0.004) -> dict:
        nodes = list(self.nodes.values())
        lats, lons = [n["lat"] for n in nodes], [n["lon"] for n in nodes]
        return {"nodes": nodes, "edges": self.edges, "segments": self.segments,
                "bounds": {"min_lat": round(min(lats) - bounds_pad, 6), "max_lat": round(max(lats) + bounds_pad, 6),
                           "min_lon": round(min(lons) - bounds_pad, 6), "max_lon": round(max(lons) + bounds_pad, 6)}}


# --- structural operations -------------------------------------------------------------
def _remove_gates(m: _Map, rng: random.Random, n: int, min_left: int) -> None:
    for venue in sorted(m.event_venues()):
        if n <= 0:
            return
        gates = m.gates_of(venue)
        while n > 0 and len(gates) > min_left:
            g = rng.choice(gates)
            feeders = [e for e in m.edges if e["dst_entity_id"] == g and m.type(e["src_entity_id"]) in ACCESS_TYPES]
            m.remove(g)
            gates.remove(g)
            # An access node that only reached the venue through this gate is
            # re-pointed at another of the venue's gates (as an operator would).
            for e in feeders:
                src = e["src_entity_id"]
                if not any(x["src_entity_id"] == src and x["edge_type"] in ("feeds", "serves") for x in m.edges):
                    m.add_edge(_edge(src, rng.choice(gates), e["edge_type"], e["transfer_coefficient"],
                                     e["travel_time_sec"] + rng.randint(60, 240)))
            m.ops.append(f"remove_gate:{g}")
            n -= 1


def _add_gates(m: _Map, rng: random.Random, n: int, counter: list[int]) -> None:
    venues = [v for v in sorted(m.event_venues()) if m.gates_of(v)]
    if not venues:
        return
    for _ in range(n):
        venue = rng.choice(venues)
        gates = m.gates_of(venue)
        caps = sorted(m.nodes[g]["nominal_capacity"] for g in gates)
        feeders = sorted({e["src_entity_id"] for e in m.edges
                          if e["dst_entity_id"] in gates and m.type(e["src_entity_id"]) in ACCESS_TYPES})
        roads, zones = m.of_type("road"), m.of_type("zone")
        if not feeders or not roads:
            return
        counter[0] += 1
        gid = f"gate_r{counter[0]}"
        m.add_node(gid, "gate", f"Gate R{counter[0]}", rng.choice(gates),
                   caps[len(caps) // 2] * rng.uniform(0.7, 1.3), rng, parent=venue)
        m.add_edge(_edge(gid, venue, "feeds", 0.9, rng.randint(150, 210)))
        for src in rng.sample(feeders, k=min(len(feeders), rng.randint(1, 2))):
            etype = "feeds" if m.type(src) == "transport_node" else "serves"
            m.add_edge(_edge(src, gid, etype, rng.uniform(0.2, 0.5), rng.randint(300, 600)))
        m.add_edge(_edge(gid, rng.choice(roads), "adjacent_to", rng.uniform(0.25, 0.4), rng.randint(280, 340)))
        if zones:
            m.add_edge(_edge(gid, rng.choice(zones), "serves", 0.3, 240))
        m.ops.append(f"add_gate:{gid}->{venue}")


def _remove_roads(m: _Map, rng: random.Random, n: int, min_left: int) -> None:
    for _ in range(n):
        roads = m.of_type("road")
        if len(roads) <= min_left:
            return
        r = rng.choice(roads)
        # Do not strand a gate or venue without a road to spill onto.
        spill_owners = {e["src_entity_id"] for e in m.edges if e["dst_entity_id"] == r and e["edge_type"] == "adjacent_to"
                        and m.type(e["src_entity_id"]) in ("gate", "venue")}
        if any(sum(1 for e in m.edges if e["src_entity_id"] == o and e["edge_type"] == "adjacent_to"
                   and m.type(e["dst_entity_id"]) == "road") <= 1 for o in spill_owners):
            continue
        m.remove(r)
        m.ops.append(f"remove_road:{r}")


def _add_roads(m: _Map, rng: random.Random, n: int, counter: list[int]) -> None:
    for _ in range(n):
        roads = m.of_type("road")
        if len(roads) < 2:
            return
        counter[0] += 1
        rid = f"road_r{counter[0]}"
        a, b = rng.sample(roads, 2)
        m.add_node(rid, "road", f"Arterial R{counter[0]}", a, rng.uniform(650, 1100), rng)
        m.add_edge(_edge(a, rid, "adjacent_to", 0.22, 240))
        m.add_edge(_edge(rid, b, "adjacent_to", 0.22, 240))
        posts = m.of_type("emergency_facility")
        if posts and rng.random() < 0.5:
            m.add_edge(_edge(rid, rng.choice(posts), "evacuates_to", rng.uniform(0.15, 0.3), rng.randint(300, 360)))
        m.ops.append(f"add_road:{rid}")


def _remove_zones(m: _Map, rng: random.Random, n: int, min_left: int) -> None:
    protected = m.event_venues()
    for _ in range(n):
        zones = [z for z in m.of_type("zone") if z not in protected]
        if len(m.of_type("zone")) <= min_left or not zones:
            return
        z = rng.choice(zones)
        m.remove(z)
        m.ops.append(f"remove_zone:{z}")


def _add_zones(m: _Map, rng: random.Random, n: int, counter: list[int]) -> None:
    for _ in range(n):
        stations, zones = m.of_type("transport_node"), m.of_type("zone")
        if not stations or not zones:
            return
        counter[0] += 1
        zid = f"zone_r{counter[0]}"
        near = rng.choice(zones)
        m.add_node(zid, "zone", f"Zone R{counter[0]}", near, rng.uniform(4000, 12000), rng)
        m.add_edge(_edge(rng.choice(stations), zid, "last_mile_to", 0.35, rng.randint(300, 700)))
        m.add_edge(_edge(zid, near, "adjacent_to", 0.25, 180))
        m.ops.append(f"add_zone:{zid}")


# --- numeric operations -------------------------------------------------------------------
def _rescale(m: _Map, rng: random.Random, p: dict) -> None:
    type_factor = {t: rng.uniform(*p["type_scale"]) for t in RESCALED_TYPES}
    for n in m.nodes.values():
        t = n["entity_type"]
        if t in type_factor:
            n["nominal_capacity"] = float(max(10.0, round(
                n["nominal_capacity"] * type_factor[t] * rng.lognormvariate(0.0, p["entity_sigma"]))))
    for e in m.edges:
        if e["edge_type"] != "substitutes_for":
            e["transfer_coefficient"] = round(min(0.95, max(0.05, e["transfer_coefficient"]
                                                            * rng.uniform(*p["coefficient_scale"]))), 4)
        e["travel_time_sec"] = int(max(30, round(e["travel_time_sec"] * rng.uniform(*p["travel_scale"]))))
    m.ops.append("rescale")


def _move_events(m: _Map, rng: random.Random, p: dict, primary_event_id: str) -> None:
    kept = []
    for ev in m.events:
        if ev["event_id"] != primary_event_id and rng.random() < p["drop_secondary_event_p"]:
            m.ops.append(f"drop_event:{ev['event_id']}")
            continue
        steps = p["event_shift_min"] // 15
        shift = timedelta(minutes=15 * rng.randint(-steps, steps))
        ev["start_time"] = _iso(_parse(ev["start_time"]) + shift)
        ev["end_time"] = _iso(_parse(ev["end_time"]) + shift)
        ev["expected_attendance"] = int(round(ev["expected_attendance"] * rng.uniform(*p["attendance_scale"])))
        kept.append(ev)
    m.events = kept
    m.ops.append("move_events")


# --- validity ----------------------------------------------------------------------------
def problems(topology: dict, events: list[dict]) -> list[str]:
    """Why this map cannot be simulated meaningfully ([] = valid)."""
    out: list[str] = []
    types = {n["entity_id"]: n["entity_type"] for n in topology["nodes"]}
    if len(types) != len(topology["nodes"]):
        out.append("duplicate entity ids")
    ids = [e["edge_id"] for e in topology["edges"]]
    if len(set(ids)) != len(ids):
        out.append("duplicate edge ids")
    edges = [e for e in topology["edges"] if e["src_entity_id"] in types and e["dst_entity_id"] in types]
    if len(edges) != len(topology["edges"]):
        out.append("dangling edges")

    # Connected (ignoring substitutes_for, which is an alternative, not a path).
    adj: dict[str, set[str]] = {e: set() for e in types}
    for e in edges:
        if e["edge_type"] != "substitutes_for":
            adj[e["src_entity_id"]].add(e["dst_entity_id"])
            adj[e["dst_entity_id"]].add(e["src_entity_id"])
    start = next(iter(adj), None)
    seen, stack = set(), [start] if start else []
    while stack:
        u = stack.pop()
        if u in seen:
            continue
        seen.add(u)
        stack.extend(adj[u] - seen)
    if seen != set(types):
        out.append(f"not connected ({len(types) - len(seen)} entities unreachable)")

    feeds_in = {g: [e for e in edges if e["dst_entity_id"] == g and e["edge_type"] in ("feeds", "serves")
                    and types[e["src_entity_id"]] in ACCESS_TYPES] for g, t in types.items() if t == "gate"}
    to_venue = {g: [e for e in edges if e["src_entity_id"] == g and e["edge_type"] == "feeds"
                    and types[e["dst_entity_id"]] == "venue"] for g in feeds_in}
    for g in feeds_in:
        if not feeds_in[g]:
            out.append(f"gate {g} has no access node feeding it")
        if not to_venue[g]:
            out.append(f"gate {g} feeds no venue")
    for r, t in types.items():
        if t == "road" and not adj[r]:
            out.append(f"road {r} is isolated")

    for ev in events:
        v = ev["venue_entity_id"]
        if v not in types:
            out.append(f"event {ev['event_id']} venue {v} missing")
            continue
        direct = any(e["dst_entity_id"] == v and types[e["src_entity_id"]] in ACCESS_TYPES
                     and e["edge_type"] in ("feeds", "serves", "last_mile_to") for e in edges)
        via_gate = any(g for g, lst in to_venue.items() if feeds_in[g] and any(e["dst_entity_id"] == v for e in lst))
        if not (direct or via_gate):
            out.append(f"event {ev['event_id']} venue {v} is unreachable")
    return out


def randomise(base_topology: dict, base_events: list[dict], seed: int, primary_event_id: str,
              params: dict | None = None, max_tries: int = 25) -> dict[str, Any]:
    """One valid random map. Returns {"topology", "events", "topology_hash",
    "seed", "ops"}; raises RuntimeError if no valid draw in `max_tries`."""
    p = {**DEFAULTS, **(params or {})}
    last: list[str] = []
    for attempt in range(max_tries):
        rng = random.Random(f"{seed}:{attempt}")
        m = _Map(base_topology, base_events)
        counter = [0]
        _remove_gates(m, rng, rng.randint(*p["remove_gates"]), p["min_gates_per_venue"])
        _add_gates(m, rng, rng.randint(*p["add_gates"]), counter)
        _remove_roads(m, rng, rng.randint(*p["remove_roads"]), p["min_roads"])
        _add_roads(m, rng, rng.randint(*p["add_roads"]), counter)
        _remove_zones(m, rng, rng.randint(*p["remove_zones"]), p["min_zones"])
        _add_zones(m, rng, rng.randint(*p["add_zones"]), counter)
        _rescale(m, rng, p)
        _move_events(m, rng, p, primary_event_id)
        topology = m.to_topology()
        last = problems(topology, m.events)
        if not last:
            return {"topology": topology, "events": m.events, "seed": seed, "attempt": attempt,
                    "topology_hash": topology_hash(topology["nodes"], topology["edges"]), "ops": m.ops}
    raise RuntimeError(f"no valid map for seed {seed} in {max_tries} tries; last problems: {last[:3]}")
