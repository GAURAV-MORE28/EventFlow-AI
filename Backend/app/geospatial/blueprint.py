"""BlueprintBuilder: raw OSM data inside a footprint → an operational Blueprint.

The blueprint is the topology the existing EventFlow flow model runs on. Node
and edge semantics are the engine's own (``entity_type`` + ``subtype``; edge
types ``feeds`` / ``adjacent_to`` / ``serves`` / ``last_mile_to`` /
``evacuates_to`` / ``substitutes_for`` / ``connects_to``), so nothing
downstream needs to know the graph was generated. Behaviour is carried by
types and attributes; IDs are opaque, deterministic hashes (``n_…``, ``e_…``).

Build steps (all deterministic — sorted inputs, no sets iterated for output,
floats rounded):

1. Roads: clip each OSM way to the footprint, split at junctions (nodes shared
   by ways, dead ends, clip ends), merge junctions closer than
   ``junction_merge_m``, deduplicate parallel segments, keep oneway direction,
   length from geometry, travel time from ``maxspeed`` or road-class default.
   Each junction is a ``road`` entity; each segment an ``adjacent_to`` edge.
2. Venue: the selected point (+ OSM polygon when mapped); capacity from the
   organiser, OSM ``capacity``, the polygon area, or a default — in that order.
3. Access points (``gate``): mapped entrances first; otherwise one per bearing
   sector that has a road approach within ``gate_search_m``. Never invented
   without a road connection.
4. Transit / parking / hotels / emergency: deduplicated, clustered (bus stops),
   capped to the nearest N (monotone in the radius), snapped to the road graph
   with a ``connects_to`` edge no longer than ``snap_max_m``.
5. Operational relations the flow model uses: access node → gate along the
   shortest walking path over the road graph (``feeds`` / ``serves`` with the
   road path attached), gate → venue, gate ↔ road, hotel → nearest transit,
   emergency coupling, substitutes, and one inferred venue-precinct zone.

Every entity and edge carries provenance: source (``osm`` / ``organizer`` /
``derived`` / ``default``), source_id, confidence, generated, inferred.
"""
from __future__ import annotations

import hashlib
import heapq
import json
import math
import re
from datetime import datetime, timezone
from typing import Any

from .footprint import Footprint, bearing_deg, destination, haversine_m
from .overpass import ATTRIBUTION, RawGeoData, point_in_ring, polygon_area_m2

ONEWAY_IMPLIED = {"motorway", "motorway_link"}
RAIL_SUBTYPES = ("metro_station", "rail_station")
BUS_SUBTYPES = ("bus_station", "bus_stop_cluster")
CLASS_RANK = ["motorway", "trunk", "primary", "secondary", "tertiary", "unclassified",
              "motorway_link", "trunk_link", "primary_link", "secondary_link", "tertiary_link"]


class BlueprintError(ValueError):
    """The footprint cannot produce an operational network (structured, user-facing)."""

    def __init__(self, code: str, message: str, detail: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}


def _hid(prefix: str, key: str) -> str:
    return f"{prefix}_{hashlib.sha1(key.encode('utf-8')).hexdigest()[:10]}"


def _r(x: float, nd: int = 7) -> float:
    return round(float(x), nd)


def _prov(source: str, source_id: str | None, confidence: str, generated: bool, inferred: bool) -> dict[str, Any]:
    return {"source": source, "source_id": source_id, "confidence": confidence,
            "generated": generated, "inferred": inferred}


def compass_sector(bearing: float) -> int:
    """0..7 = N, NE, E, SE, S, SW, W, NW; each sector centred on its direction."""
    return int(((bearing + 22.5) % 360.0) // 45.0)


def nearest_on_ring(lat: float, lon: float, ring: list[list[float]]) -> tuple[float, float, float]:
    """(distance_m, lat, lon) of the closest point on a polygon boundary (local planar projection)."""
    k = 111_320.0
    c = math.cos(math.radians(lat))
    px, py = 0.0, 0.0
    best = None
    for (a_lat, a_lon), (b_lat, b_lon) in zip(ring, ring[1:] + ring[:1]):
        ax, ay = (a_lon - lon) * k * c, (a_lat - lat) * k
        bx, by = (b_lon - lon) * k * c, (b_lat - lat) * k
        dx, dy = bx - ax, by - ay
        L = dx * dx + dy * dy
        t = 0.0 if L == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L))
        qx, qy = ax + t * dx, ay + t * dy
        d = math.hypot(qx - px, qy - py)
        if best is None or d < best[0] - 1e-9:
            best = (d, lat + qy / k, lon + qx / (k * c))
    return best if best else (0.0, lat, lon)


def parse_speed_kmh(value: str | None) -> float | None:
    if not value:
        return None
    m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*(mph|km/h|kmh|kph)?\s*$", str(value).lower())
    if not m:
        return None
    v = float(m.group(1))
    if m.group(2) == "mph":
        v *= 1.609344
    return v if 3.0 <= v <= 150.0 else None


CAPACITY_TAGS = ("capacity:rooms", "rooms", "capacity:beds", "beds", "capacity:persons")


def parse_capacity(value: Any, hi: int = 20000) -> int | None:
    """A mapped capacity tag -> positive int, or None. Accepts "235", " 235 ", "235.0";
    rejects "unknown", "-3", "0", "200-250", "~200", "1e3" and anything above `hi`."""
    if value is None:
        return None
    m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*$", str(value))
    if not m:
        return None
    v = float(m.group(1))
    if v < 0.5 or v > hi:
        return None
    return int(round(v))


def hotel_capacity(tags: dict[str, str], subtype: str, guests_per_room: float,
                   default_rooms: dict[str, int]) -> dict[str, Any]:
    """Room / bed / guest capacity of one mapped hotel, with provenance.

    Precedence (rooms are the simulation's unit): capacity:rooms, rooms (mapped,
    high confidence) -> rooms derived from capacity:beds / beds / capacity:persons
    (medium: the bed count is mapped, the rooms are computed) -> a type default
    (low: estimate). Beds are kept as beds, never relabelled as rooms.
    """
    rooms, rooms_src = None, None
    for key, src in (("capacity:rooms", "osm_capacity_rooms"), ("rooms", "osm_rooms")):
        rooms = parse_capacity(tags.get(key))
        if rooms:
            rooms_src = src
            break
    beds, bed_src = None, None
    for key, src in (("capacity:beds", "osm_capacity_beds"), ("beds", "osm_beds"),
                     ("capacity:persons", "osm_capacity_persons")):
        beds = parse_capacity(tags.get(key))
        if beds:
            bed_src = src
            break
    gpr = max(float(guests_per_room), 0.1)
    if rooms:
        conf = "high"
    elif beds:
        rooms, rooms_src, conf = max(1, math.ceil(beds / gpr)), f"derived_from_{bed_src}", "medium"
    else:
        rooms, rooms_src, conf = int(default_rooms.get(subtype, 40)), "derived_estimate", "low"
    if beds and rooms_src.startswith("osm_"):
        guests, guests_src = min(rooms * gpr, float(beds)), "min(osm_rooms x guests_per_room, osm_beds)"
    elif beds:
        guests, guests_src = float(beds), bed_src
    else:
        guests, guests_src = rooms * gpr, f"{rooms_src} x guests_per_room"
    return {
        "room_capacity": int(rooms), "rooms_source": rooms_src, "rooms_confidence": conf,
        "bed_capacity": beds, "bed_source": bed_src,
        "effective_guest_capacity": round(guests, 1), "guest_capacity_source": guests_src,
        "capacity_tags": {k: tags[k] for k in CAPACITY_TAGS if k in tags},
    }


def parse_int(value: str | None, lo: int = 1, hi: int = 10**6) -> int | None:
    if value is None:
        return None
    m = re.match(r"^\s*(\d+)\s*$", str(value))
    if not m:
        return None
    v = int(m.group(1))
    return v if lo <= v <= hi else None


class BlueprintBuilder:
    def __init__(self, cfg: dict[str, Any], segments: list[dict[str, Any]]) -> None:
        self.c = cfg
        self.segments = segments

    # --- helpers ----------------------------------------------------------------------------------
    def _cfg(self, key: str, default: Any = None) -> Any:
        return self.c.get(key, default)

    # --- 1. roads ------------------------------------------------------------------------------------
    def _road_graph(self, raw: RawGeoData, fp: Footprint, warnings: list[str]) -> dict[str, Any]:
        """Returns junctions {rep_osm_id: {...}}, segments [...]."""
        pieces: list[dict[str, Any]] = []
        for way in raw.roads:                       # sorted by osm id
            run: list[tuple[int, tuple[float, float]]] = []
            for nid, pt in zip(way["nodes"], way["geometry"]):
                if fp.contains(pt[0], pt[1]):
                    run.append((nid, pt))
                else:
                    if len(run) >= 2:
                        pieces.append({"way": way, "nodes": run})
                    run = []
            if len(run) >= 2:
                pieces.append({"way": way, "nodes": run})
        if not pieces:
            return {"junctions": {}, "segments": []}

        use: dict[int, int] = {}
        pos: dict[int, tuple[float, float]] = {}
        for p in pieces:
            ns = p["nodes"]
            for i, (nid, pt) in enumerate(ns):
                use[nid] = use.get(nid, 0) + (1 if i in (0, len(ns) - 1) else 2)
                pos[nid] = pt
        junction = {nid for nid, c in use.items() if c != 2}
        for p in pieces:                           # piece ends are always junctions
            junction.add(p["nodes"][0][0])
            junction.add(p["nodes"][-1][0])

        # Merge junctions closer than junction_merge_m (grid bucketed, deterministic union-find).
        merge_m = float(self._cfg("junction_merge_m", 20))
        ids = sorted(junction)
        parent = {i: i for i in ids}

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x
        cell = merge_m / 111_000.0 or 1e-4
        grid: dict[tuple[int, int], list[int]] = {}
        for nid in ids:
            la, lo = pos[nid]
            grid.setdefault((int(la // cell), int(lo // cell)), []).append(nid)
        for nid in ids:
            la, lo = pos[nid]
            gx, gy = int(la // cell), int(lo // cell)
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for other in grid.get((gx + dx, gy + dy), []):
                        if other <= nid:
                            continue
                        if haversine_m(la, lo, *pos[other]) <= merge_m:
                            a, b = find(nid), find(other)
                            if a != b:
                                parent[max(a, b)] = min(a, b)
        members: dict[int, list[int]] = {}
        for nid in ids:
            members.setdefault(find(nid), []).append(nid)
        rep = {nid: find(nid) for nid in ids}
        junctions: dict[int, dict[str, Any]] = {}
        for r, ms in sorted(members.items()):
            junctions[r] = {"osm_ids": ms, "lat": _r(sum(pos[m][0] for m in ms) / len(ms)),
                            "lon": _r(sum(pos[m][1] for m in ms) / len(ms))}

        segs: dict[tuple[int, int], dict[str, Any]] = {}
        for p in pieces:
            way, ns = p["way"], p["nodes"]
            tags = way["tags"]
            hw = tags.get("highway", "unclassified")
            oneway_tag = tags.get("oneway", "")
            reverse = oneway_tag == "-1"
            oneway = oneway_tag in ("yes", "true", "1", "-1") or (
                (hw in ONEWAY_IMPLIED or tags.get("junction") == "roundabout") and oneway_tag != "no")
            start = 0
            for i in range(1, len(ns)):
                if ns[i][0] not in junction and i != len(ns) - 1:
                    continue
                chunk = ns[start:i + 1]
                start = i
                a, b = rep[chunk[0][0]], rep[chunk[-1][0]]
                if a == b:
                    continue
                pts = [c[1] for c in chunk]
                length = sum(haversine_m(*pts[k], *pts[k + 1]) for k in range(len(pts) - 1))
                if reverse:
                    a, b, pts = b, a, list(reversed(pts))
                key = (a, b) if oneway else (min(a, b), max(a, b))
                geom = pts if (not oneway and key == (a, b)) or oneway else list(reversed(pts))
                cand = {"a": key[0], "b": key[1], "oneway": oneway, "length_m": length, "tags": tags,
                        "way_id": way["osm_id"], "geometry": geom, "highway": hw}
                old = segs.get(key)
                if old is None or (round(length, 3), way["osm_id"]) < (round(old["length_m"], 3), old["way_id"]):
                    if old is not None:
                        cand["merged_way_ids"] = sorted(set(old.get("merged_way_ids", [old["way_id"]]) + [old["way_id"]]))
                    segs[key] = cand
        return {"junctions": junctions, "segments": [segs[k] for k in sorted(segs)]}

    def _speed(self, seg: dict) -> tuple[float, str, str]:
        v = parse_speed_kmh(seg["tags"].get("maxspeed"))
        if v is not None:
            return v, "osm_maxspeed", "medium"
        table = self._cfg("class_speed_kmh", {})
        return float(table.get(seg["highway"], 30)), "derived_highway_class", "low"

    def _lanes(self, seg: dict) -> tuple[int, str]:
        v = parse_int(seg["tags"].get("lanes"), 1, 12)
        if v is not None:
            return (v if seg["oneway"] else max(1, v // 2)), "osm_lanes"
        return int(self._cfg("class_lanes", {}).get(seg["highway"], 1)), "derived_highway_class"

    # --- main ---------------------------------------------------------------------------------------
    def build(self, venue: dict[str, Any], fp: Footprint, raw: RawGeoData, *, venue_capacity: int | None = None,
              max_gates: int | None = None, data_source: str = "live_osm") -> dict[str, Any]:
        warnings: list[str] = []
        walk = float(self._cfg("walk_speed_mps", 1.3))
        rg = self._road_graph(raw, fp, warnings)
        if not rg["junctions"]:
            raise BlueprintError("NO_ROAD_NETWORK", f"No usable roads of the configured classes were found within "
                                 f"{int(fp.radius_m)} m of the venue.", {"radius_m": fp.radius_m})

        nodes: dict[str, dict[str, Any]] = {}
        edges: dict[str, dict[str, Any]] = {}

        def add_node(node: dict[str, Any]) -> str:
            if node["entity_id"] in nodes:
                raise BlueprintError("ID_COLLISION", f"generated id collision {node['entity_id']}")
            nodes[node["entity_id"]] = node
            return node["entity_id"]

        def add_edge(src: str, dst: str, etype: str, travel: float, *, key: str, distance: float | None,
                     coeff: float = 0.0, subst: float = 0.0, prov: dict, travel_source: str,
                     directionality: str = "oneway", geometry: list | None = None, capacity: float | None = None,
                     path: list[str] | None = None) -> None:
            if src == dst:
                return
            eid = _hid("e", f"{src}|{dst}|{etype}|{key}")
            if eid in edges:
                return
            edges[eid] = {
                "edge_id": eid, "src_entity_id": src, "dst_entity_id": dst, "edge_type": etype,
                "transfer_coefficient": round(float(coeff), 4), "travel_time_sec": max(1, int(math.ceil(travel))),
                "substitutability": round(float(subst), 4),
                "distance_m": None if distance is None else round(float(distance), 1),
                "capacity_per_min": None if capacity is None else round(float(capacity), 1),
                "directionality": directionality, "travel_time_source": travel_source,
                "geometry": geometry, "via_entity_ids": path, "provenance": prov,
            }

        # --- road entities + segments ------------------------------------------------------------
        road_id: dict[int, str] = {}
        incident: dict[int, list[dict]] = {}
        for s in rg["segments"]:
            incident.setdefault(s["a"], []).append(s)
            incident.setdefault(s["b"], []).append(s)
        ppl_per_lane_m = float(self._cfg("road_people_per_lane_m", 1.5))
        for rep, j in rg["junctions"].items():
            segs = incident.get(rep, [])
            if not segs:
                continue
            names = sorted({s["tags"].get("name") for s in segs if s["tags"].get("name")})
            best = min((s["highway"] for s in segs), key=lambda h: CLASS_RANK.index(h) if h in CLASS_RANK else 99)
            storage = sum(s["length_m"] / 2.0 * self._lanes(s)[0] * ppl_per_lane_m for s in segs)
            mean_len = sum(s["length_m"] for s in segs) / len(segs)
            if len(names) >= 2:
                label = f"{names[0]} × {names[1]}"
            elif names:
                label = f"{names[0]} junction"
            else:
                label = f"{best.replace('_', ' ').title()} junction"
            nid = add_node({
                "entity_id": _hid("n", f"osm:node:{rep}"), "entity_type": "road", "subtype": best,
                "display_name": label[:120], "lat": j["lat"], "lon": j["lon"],
                "nominal_capacity": float(max(50.0, round(storage, 0))),
                "capacity_source": "derived_road_geometry", "capacity_confidence": "low", "parent_id": None,
                "provenance": _prov("osm", f"node/{rep}", "high", True, False),
                "meta": {"osm_node_ids": j["osm_ids"], "degree": len(segs), "road_names": names[:4],
                         "dwell_sec": round(mean_len / 2.0 / walk * 2.0, 1)},
            })
            road_id[rep] = nid
        lane_cap = float(self._cfg("lane_capacity_veh_per_min", 30))
        for s in rg["segments"]:
            a, b = road_id.get(s["a"]), road_id.get(s["b"])
            if not a or not b:
                continue
            kmh, tsrc, tconf = self._speed(s)
            lanes, lsrc = self._lanes(s)
            geom = [[_r(p[1]), _r(p[0])] for p in s["geometry"]]
            prov = _prov("osm", f"way/{s['way_id']}", "high", True, False)
            prov["travel_time_confidence"] = tconf
            prov["lanes_source"] = lsrc
            add_edge(a, b, "adjacent_to", s["length_m"] / (kmh / 3.6), key=f"way{s['way_id']}", distance=s["length_m"],
                     coeff=0.22, prov=prov, travel_source=tsrc,
                     directionality="oneway" if s["oneway"] else "bidirectional", geometry=geom,
                     capacity=lanes * lane_cap)

        # Road adjacency for path search (walking ignores oneway; driving respects it).
        walk_adj: dict[str, list[tuple[str, float]]] = {}
        drive_adj: dict[str, list[tuple[str, float]]] = {}
        for e in edges.values():
            a, b, d = e["src_entity_id"], e["dst_entity_id"], e["distance_m"]
            walk_adj.setdefault(a, []).append((b, d))
            walk_adj.setdefault(b, []).append((a, d))
            drive_adj.setdefault(a, []).append((b, d))
            if e["directionality"] == "bidirectional":
                drive_adj.setdefault(b, []).append((a, d))
        for k in walk_adj:
            walk_adj[k].sort()

        # Components: drop tiny fragments (clip artefacts); the rest stay.
        comp: dict[str, int] = {}
        comps: list[list[str]] = []
        for start in sorted(walk_adj):
            if start in comp:
                continue
            stack, members = [start], []
            comp[start] = len(comps)
            while stack:
                x = stack.pop()
                members.append(x)
                for y, _ in walk_adj.get(x, []):
                    if y not in comp:
                        comp[y] = len(comps)
                        stack.append(y)
            comps.append(sorted(members))
        min_comp = int(self._cfg("min_component_nodes", 3))
        dropped = [c for c in comps if len(c) < min_comp]
        for c in dropped:
            for nid in c:
                nodes.pop(nid, None)
                walk_adj.pop(nid, None)
                drive_adj.pop(nid, None)
        if dropped:
            edges = {k: e for k, e in edges.items() if e["src_entity_id"] in nodes and e["dst_entity_id"] in nodes}
            warnings.append(f"{len(dropped)} road fragment(s) with fewer than {min_comp} junctions were dropped "
                            "(roads cut by the footprint edge).")
        road_nodes = sorted(n for n, v in nodes.items() if v["entity_type"] == "road")
        if not road_nodes:
            raise BlueprintError("NO_ROAD_NETWORK", "The roads inside the footprint are too fragmented to form a network.")

        def nearest_road(lat: float, lon: float, max_m: float) -> tuple[str, float] | None:
            best = None
            for rid in road_nodes:
                n = nodes[rid]
                d = haversine_m(lat, lon, n["lat"], n["lon"])
                if d <= max_m and (best is None or (round(d, 3), rid) < (round(best[1], 3), best[0])):
                    best = (rid, d)
            return best

        # --- venue ----------------------------------------------------------------------------------
        ring = raw.venue_geometry
        venue_tags = (raw.venue_osm or {}).get("tags", {})
        area = polygon_area_m2(ring) if ring else None
        osm_cap = parse_int(venue_tags.get("capacity")) or venue.get("osm_capacity")
        if venue_capacity:
            vcap, vsrc, vconf = int(venue_capacity), "organizer", "high"
        elif osm_cap:
            vcap, vsrc, vconf = int(osm_cap), "osm_attribute", "medium"
        elif area:
            vcap = int(round(area * float(self._cfg("venue_people_per_m2", 1.2)), -2) or 100)
            vsrc, vconf = "derived_venue_area", "low"
        else:
            vcap, vsrc, vconf = int(self._cfg("venue_default_capacity", 20000)), "default_estimate", "low"
            warnings.append("No venue polygon or capacity in OSM; the venue capacity is a default estimate. "
                            "Provide venue_capacity for a meaningful simulation.")
        venue_src = "organizer" if venue["source"] == "coordinates" else "osm"
        venue_id = add_node({
            "entity_id": _hid("n", f"venue:{venue['venue_id']}"), "entity_type": "venue", "subtype": "event_venue",
            "display_name": venue["display_name"], "lat": venue["lat"], "lon": venue["lon"],
            "nominal_capacity": float(vcap), "capacity_source": vsrc, "capacity_confidence": vconf, "parent_id": None,
            "provenance": _prov(venue_src, venue.get("source_id"), venue.get("confidence", "medium"), True, False),
            "meta": {"venue_osm": {k: raw.venue_osm[k] for k in ("osm_type", "osm_id")} if raw.venue_osm else None,
                     "footprint_area_m2": None if area is None else round(area, 1)},
        })

        def dist_to_venue(lat: float, lon: float) -> float:
            if ring:
                if point_in_ring(lat, lon, ring):
                    return 0.0
                return nearest_on_ring(lat, lon, ring)[0]
            return haversine_m(lat, lon, venue["lat"], venue["lon"])

        # --- gates (access points) --------------------------------------------------------------------
        gate_cap_max = int(max_gates or self._cfg("max_gates", 8))
        compass = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
        gate_candidates: list[dict[str, Any]] = []
        near_m = float(self._cfg("entrance_max_m", 250)) if ring else 300.0
        for ent in raw.entrances:
            kind = ent["tags"].get("entrance", "yes")
            if kind in ("exit", "service", "emergency") or ent["tags"].get("access") in ("private", "no"):
                continue
            d_ent = dist_to_venue(ent["lat"], ent["lon"])
            if d_ent > near_m:
                continue
            gate_candidates.append({"lat": ent["lat"], "lon": ent["lon"], "rank": 0 if kind == "main" else 1,
                                    "key": f"osm:node:{ent['osm_id']}", "source": "osm",
                                    "source_id": f"node/{ent['osm_id']}", "label": ent["tags"].get("name") or
                                    ent["tags"].get("ref"),
                                    "evidence": f"mapped entrance ({kind}, {int(d_ent)} m from the venue outline)"})
        # merge entrances closer than gate_merge_m
        merged: list[dict[str, Any]] = []
        for g in sorted(gate_candidates, key=lambda g: (g["rank"], g["key"])):
            if any(haversine_m(g["lat"], g["lon"], m["lat"], m["lon"]) <= float(self._cfg("gate_merge_m", 80))
                   for m in merged):
                continue
            merged.append(g)
        # Road approaches: one per compass sector that has a road junction within
        # gate_search_m of the venue. Used when no entrance is mapped, and to cover
        # sectors without a mapped entrance when fewer than min_access_points exist.
        min_access = int(self._cfg("min_access_points", 4))
        mapped = len(merged)
        if mapped < min(min_access, gate_cap_max):
            search = float(self._cfg("gate_search_m", 350))
            sectors: dict[int, tuple[float, str]] = {}
            for rid in road_nodes:
                n = nodes[rid]
                d = dist_to_venue(n["lat"], n["lon"])
                if d > search:
                    continue
                sec = compass_sector(bearing_deg(venue["lat"], venue["lon"], n["lat"], n["lon"]))
                if sec not in sectors or (round(d, 3), rid) < (round(sectors[sec][0], 3), sectors[sec][1]):
                    sectors[sec] = (d, rid)
            covered = {compass_sector(bearing_deg(venue["lat"], venue["lon"], g["lat"], g["lon"])) for g in merged}
            approaches = []
            for sec in sorted(sectors):
                if sec in covered:
                    continue
                d, rid = sectors[sec]
                n = nodes[rid]
                brg = bearing_deg(venue["lat"], venue["lon"], n["lat"], n["lon"])
                if ring:
                    _, glat, glon = nearest_on_ring(n["lat"], n["lon"], ring)
                else:
                    glat, glon = destination(venue["lat"], venue["lon"], brg, min(80.0, max(20.0, d / 2)))
                approaches.append((round(d, 3), sec, {
                    "lat": _r(glat), "lon": _r(glon), "rank": 2, "key": f"approach:{rid}",
                    "source": "derived", "source_id": rid, "label": f"Access {compass[sec]}", "snap": rid,
                    "evidence": f"road approach {int(d)} m from the venue ({compass[sec]})"}))
            need = (gate_cap_max if not mapped else min(min_access, gate_cap_max)) - mapped
            for _, _, g in sorted(approaches, key=lambda a: (a[0], a[1]))[:max(0, need)]:
                merged.append(g)
        gate_mode = ("mapped_entrances" if mapped and len(merged) == mapped else
                     "road_approaches" if not mapped else "mapped_entrances+road_approaches")
        merged.sort(key=lambda g: (g["rank"], g["key"]))
        gates: list[str] = []
        gate_snap: dict[str, tuple[str, float]] = {}
        for g in merged:
            if len(gates) >= gate_cap_max:
                break
            snap = (g["snap"], dist_to_venue(nodes[g["snap"]]["lat"], nodes[g["snap"]]["lon"])) if g.get("snap") else \
                nearest_road(g["lat"], g["lon"], float(self._cfg("gate_snap_max_m", 500)))
            if snap is None:
                warnings.append(f"Access point {g['evidence']} has no road within "
                                f"{int(self._cfg('gate_snap_max_m', 500))} m and was not used.")
                continue
            sd = haversine_m(g["lat"], g["lon"], nodes[snap[0]]["lat"], nodes[snap[0]]["lon"])
            label = g["label"] or f"Access {compass[compass_sector(bearing_deg(venue['lat'], venue['lon'], g['lat'], g['lon']))]}"
            gid = add_node({
                "entity_id": _hid("n", f"gate:{g['key']}"), "entity_type": "gate", "subtype": "access_point",
                "display_name": f"{label}"[:120], "lat": g["lat"], "lon": g["lon"], "nominal_capacity": 1.0,
                "capacity_source": "derived_venue_capacity", "capacity_confidence": "low", "parent_id": venue_id,
                "provenance": _prov(g["source"], g["source_id"], "medium" if g["source"] == "osm" else "low", True, True),
                "meta": {"evidence": g["evidence"], "gate_inference": gate_mode},
            })
            gates.append(gid)
            gate_snap[gid] = (snap[0], sd)
        if not gates:
            raise BlueprintError("NO_ACCESS", f"Blueprint could not be built: the selected venue has no connected road "
                                 f"access within {int(self._cfg('gate_search_m', 350))} m.", {"radius_m": fp.radius_m})
        # Two unnamed access points in the same sector must not share a label.
        seen_labels: dict[str, int] = {}
        for gid in gates:
            base = nodes[gid]["display_name"]
            seen_labels[base] = seen_labels.get(base, 0) + 1
        dup = {k for k, v in seen_labels.items() if v > 1}
        counter: dict[str, int] = {}
        for gid in gates:
            base = nodes[gid]["display_name"]
            if base in dup:
                counter[base] = counter.get(base, 0) + 1
                nodes[gid]["display_name"] = f"{base} {counter[base]}"
        gcap = max(100.0, round(vcap * float(self._cfg("gate_capacity_ratio", 0.2)) / len(gates), 0))
        for gid in gates:
            nodes[gid]["nominal_capacity"] = float(gcap)
            snap, sd = gate_snap[gid]
            gv = haversine_m(nodes[gid]["lat"], nodes[gid]["lon"], venue["lat"], venue["lon"])
            dprov = _prov("derived", None, "medium", True, True)
            add_edge(gid, venue_id, "feeds", max(60.0, gv / walk), key="gv", distance=gv, coeff=0.9, prov=dprov,
                     travel_source="derived_geodesic_walk")
            add_edge(snap, gid, "feeds", max(10.0, sd / walk), key="approach", distance=sd, coeff=1.0,
                     prov=dprov, travel_source="derived_geodesic_walk", directionality="oneway")
            add_edge(gid, snap, "adjacent_to", max(10.0, sd / walk), key="spill", distance=sd, coeff=1.0,
                     prov=dprov, travel_source="derived_geodesic_walk")

        # --- points of interest ------------------------------------------------------------------------
        def nearest_n(items: list[dict], n: int) -> list[dict]:
            ranked = sorted(items, key=lambda x: (round(haversine_m(venue["lat"], venue["lon"], x["lat"], x["lon"]), 3),
                                                  x["key"]))
            return ranked[:n]

        def display(tags: dict, fallback: str) -> str:
            return (tags.get("name") or tags.get("name:en") or tags.get("ref") or fallback)[:120]

        def snap_or_warn(label: str, lat: float, lon: float) -> tuple[str, float] | None:
            s = nearest_road(lat, lon, float(self._cfg("snap_max_m", 400)))
            if s is None:
                warnings.append(f"{label} is more than {int(self._cfg('snap_max_m', 400))} m from the road network "
                                "and was not included.")
            return s

        # transit: dedupe stations, cluster bus stops
        stations = [t for t in raw.transit if t["subtype"] in RAIL_SUBTYPES + ("bus_station",)]
        stops = [t for t in raw.transit if t["subtype"] == "bus_stop"]
        station_groups: list[dict] = []
        smerge = float(self._cfg("station_merge_m", 150))
        for t in sorted(stations, key=lambda t: (RAIL_SUBTYPES.index(t["subtype"]) if t["subtype"] in RAIL_SUBTYPES
                                                  else 9, t["osm_type"], t["osm_id"])):
            name = t["tags"].get("name")
            grp = next((g for g in station_groups if g["subtype"] == t["subtype"] or (
                t["subtype"] in RAIL_SUBTYPES and g["subtype"] in RAIL_SUBTYPES)
                and haversine_m(t["lat"], t["lon"], g["lat"], g["lon"]) <= (smerge if (name and name == g["name"]) else 60.0)),
                None)
            if grp:
                grp["members"].append(t)
                continue
            station_groups.append({"subtype": t["subtype"], "lat": t["lat"], "lon": t["lon"], "name": name,
                                   "members": [t], "tags": t["tags"]})
        clusters: list[dict] = []
        bus_m = float(self._cfg("bus_cluster_m", 250))
        for t in stops:                                       # sorted by osm id
            grp = next((c for c in clusters if haversine_m(t["lat"], t["lon"], c["lat0"], c["lon0"]) <= bus_m), None)
            if grp:
                grp["members"].append(t)
            else:
                clusters.append({"lat0": t["lat"], "lon0": t["lon"], "members": [t]})
        transit_items = []
        for g in station_groups:
            m = g["members"]
            transit_items.append({"key": "osm:" + "+".join(f"{x['osm_type']}/{x['osm_id']}" for x in m),
                                  "subtype": g["subtype"], "lat": g["lat"], "lon": g["lon"],
                                  "name": display(g["tags"], g["subtype"].replace("_", " ").title()),
                                  "members": m, "aggregate": len(m) > 1})
        for c in clusters:
            m = c["members"]
            names = sorted({x["tags"].get("name") for x in m if x["tags"].get("name")})
            transit_items.append({"key": "osm:" + "+".join(f"{x['osm_type']}/{x['osm_id']}" for x in m),
                                  "subtype": "bus_stop_cluster",
                                  "lat": _r(sum(x["lat"] for x in m) / len(m)), "lon": _r(sum(x["lon"] for x in m) / len(m)),
                                  "name": (names[0] if names else "Bus stops") + (f" (+{len(m) - 1} stops)" if len(m) > 1 else ""),
                                  "members": m, "aggregate": True})
        tcap = self._cfg("transit_capacity", {})
        access: list[str] = []
        snaps: dict[str, tuple[str, float]] = {}
        # Caps per mode group (nearest-N within each, so still monotone in the radius):
        # dozens of bus-stop clusters must never crowd out the rail/metro stations.
        rail_items = [t for t in transit_items if t["subtype"] in RAIL_SUBTYPES]
        bus_items = [t for t in transit_items if t["subtype"] not in RAIL_SUBTYPES]
        chosen = nearest_n(rail_items, int(self._cfg("max_rail_stations", 20))) + \
            nearest_n(bus_items, int(self._cfg("max_transit", 40)))
        for t in sorted(chosen, key=lambda t: t["key"]):
            s = snap_or_warn(t["name"], t["lat"], t["lon"])
            if s is None:
                continue
            base = float(tcap.get(t["subtype"], 300))
            cap = min(1500.0, base * len(t["members"])) if t["subtype"] == "bus_stop_cluster" else base
            tid = add_node({
                "entity_id": _hid("n", f"transit:{t['key']}"), "entity_type": "transport_node", "subtype": t["subtype"],
                "display_name": t["name"], "lat": t["lat"], "lon": t["lon"], "nominal_capacity": cap,
                "capacity_source": "estimated_subtype_default", "capacity_confidence": "low", "parent_id": None,
                "provenance": _prov("osm", ";".join(f"{x['osm_type']}/{x['osm_id']}" for x in t["members"])[:400],
                                    "high", True, t["aggregate"]),
                "meta": {"osm_members": len(t["members"])},
            })
            access.append(tid)
            snaps[tid] = s
        parking_items = [{"key": f"osm:{p['osm_type']}/{p['osm_id']}", **p} for p in raw.parking]
        for p in nearest_n(parking_items, int(self._cfg("max_parking", 25))):
            s = snap_or_warn(display(p["tags"], "Parking"), p["lat"], p["lon"])
            if s is None:
                continue
            cap_tag = parse_int(p["tags"].get("capacity"), 1, 100000)
            if cap_tag:
                cap, csrc, cconf = float(cap_tag), "osm_attribute", "medium"
            elif p.get("area_m2") and p["area_m2"] >= 100:
                cap, csrc, cconf = float(max(10, round(p["area_m2"] / float(self._cfg("parking_m2_per_vehicle", 25))))), \
                    "derived_area", "low"
            else:
                cap, csrc, cconf = float(self._cfg("parking_default_capacity", 100)), "default_estimate", "low"
            pid = add_node({
                "entity_id": _hid("n", f"parking:{p['key']}"), "entity_type": "parking", "subtype": p["subtype"],
                "display_name": display(p["tags"], "Parking"), "lat": p["lat"], "lon": p["lon"], "nominal_capacity": cap,
                "capacity_source": csrc, "capacity_confidence": cconf, "parent_id": None,
                "provenance": _prov("osm", f"{p['osm_type']}/{p['osm_id']}", "high", True, False),
                "meta": {"area_m2": p.get("area_m2")},
            })
            access.append(pid)
            snaps[pid] = s

        # --- shortest walking paths: access node → every gate ------------------------------------------
        def dijkstra(src: str, adj: dict[str, list[tuple[str, float]]]) -> tuple[dict[str, float], dict[str, str]]:
            dist, prev = {src: 0.0}, {}
            heap = [(0.0, src)]
            while heap:
                d, u = heapq.heappop(heap)
                if d > dist.get(u, 1e18):
                    continue
                for v, w in adj.get(u, []):
                    nd = d + w
                    if nd < dist.get(v, 1e18) - 1e-9 or (abs(nd - dist.get(v, 1e18)) <= 1e-9 and u < prev.get(v, "~")):
                        dist[v] = nd
                        prev[v] = u
                        heapq.heappush(heap, (nd, v))
            return dist, prev

        def path_to(prev: dict[str, str], src: str, dst: str) -> list[str]:
            out = [dst]
            while out[-1] != src:
                out.append(prev[out[-1]])
            return list(reversed(out))

        tau = float(self._cfg("gate_choice_tau_sec", 300))
        max_walk = float(self._cfg("access_max_walk_m", 2500))
        gate_roads = {g: gate_snap[g][0] for g in gates}
        unreachable = 0
        path_cache: dict[str, tuple[dict[str, float], dict[str, str]]] = {}

        def walk_from(road: str) -> tuple[dict[str, float], dict[str, str]]:
            if road not in path_cache:
                path_cache[road] = dijkstra(road, walk_adj)
            return path_cache[road]

        for aid in access:
            road, sd = snaps[aid]
            dist, prev = walk_from(road)
            options = []
            for g in gates:
                gr = gate_roads[g]
                if gr not in dist:
                    continue
                total = sd + dist[gr] + gate_snap[g][1]
                if total > max_walk:
                    continue
                options.append((total, g, path_to(prev, road, gr)))
            if not options:
                unreachable += 1
                continue
            ws = [math.exp(-(o[0] / walk) / tau) for o in options]
            tot = sum(ws) or 1.0
            etype = "serves" if nodes[aid]["entity_type"] == "parking" else "feeds"
            for (total, g, path), w in zip(options, ws):
                add_edge(aid, g, etype, total / walk, key="route", distance=total, coeff=w / tot,
                         prov=_prov("derived", "shortest_walking_path", "medium", True, True),
                         travel_source="derived_shortest_path_walk", path=path)
        if unreachable:
            warnings.append(f"{unreachable} station/parking node(s) are beyond {int(max_walk)} m walk (or on a "
                            "disconnected road fragment) and are not direct arrival points for the venue.")
        for aid in access:
            road, sd = snaps[aid]
            add_edge(aid, road, "connects_to", sd / walk, key="snap", distance=sd,
                     prov=_prov("derived", "snap", "medium", True, True), travel_source="derived_geodesic_walk",
                     directionality="bidirectional")
        if not any(e["edge_type"] in ("feeds", "serves") and nodes[e["src_entity_id"]]["entity_type"] in
                   ("transport_node", "parking") for e in edges.values()):
            raise BlueprintError("NO_ARRIVAL_ACCESS", "Blueprint could not be built: no station, bus stop or car park "
                                 f"within {int(max_walk)} m walk connects to the venue's access points.",
                                 {"radius_m": fp.radius_m})

        # --- hotels -------------------------------------------------------------------------------------
        hotel_items = [{"key": f"osm:{h['osm_type']}/{h['osm_id']}", **h} for h in raw.hotels]
        properties: list[dict[str, Any]] = []
        defaults_rooms = self._cfg("hotel_default_rooms", {})
        gpr = float(self._cfg("guests_per_room", 2.2))
        stars_tier = {1: "budget", 2: "budget", 3: "midscale", 4: "upscale", 5: "luxury"}
        transit_nodes = [a for a in access if nodes[a]["entity_type"] == "transport_node"]
        for h in nearest_n(hotel_items, int(self._cfg("max_hotels", 30))):
            label = display(h["tags"], h["subtype"].replace("_", " ").title())
            s = snap_or_warn(label, h["lat"], h["lon"])
            if s is None:
                continue
            cap = hotel_capacity(h["tags"], h["subtype"], gpr, defaults_rooms)
            rooms, rsrc, rconf = cap["room_capacity"], cap["rooms_source"], cap["rooms_confidence"]
            hid = add_node({
                "entity_id": _hid("n", f"hotel:{h['key']}"), "entity_type": "hotel", "subtype": h["subtype"],
                "display_name": label, "lat": h["lat"], "lon": h["lon"], "nominal_capacity": float(rooms),
                "capacity_source": rsrc, "capacity_confidence": rconf, "parent_id": None,
                "provenance": _prov("osm", f"{h['osm_type']}/{h['osm_id']}", "high", True, False),
                "meta": {"stars": h["tags"].get("stars"), "capacity_tags": cap["capacity_tags"],
                         **{k: h["tags"][k] for k in ("brand", "operator", "website") if h["tags"].get(k)}},
            })
            road, sd = s
            add_edge(hid, road, "connects_to", sd / walk, key="snap", distance=sd,
                     prov=_prov("derived", "snap", "medium", True, True), travel_source="derived_geodesic_walk",
                     directionality="bidirectional")
            # nearest transit by walking distance over the road graph (else nearest arrival access of any kind)
            dist, _ = walk_from(road)
            reach = []
            for a in (transit_nodes or access):
                if snaps[a][0] in dist:
                    reach.append((sd + dist[snaps[a][0]] + snaps[a][1], a))
            reach.sort()
            station, walk_sec = (reach[0][1], reach[0][0] / walk) if reach else (None, None)
            if station:
                add_edge(hid, station, "last_mile_to", walk_sec, key="lastmile", distance=reach[0][0], coeff=0.4,
                         prov=_prov("derived", "shortest_walking_path", "medium", True, True),
                         travel_source="derived_shortest_path_walk")
            stars = parse_int(h["tags"].get("stars"), 1, 5)
            properties.append({
                "property_id": _hid("p", h["key"]), "name": label, "cluster_entity_id": hid,
                "zone": (h["tags"].get("addr:suburb") or h["tags"].get("addr:city") or "Within footprint")[:60],
                "lat": h["lat"], "lon": h["lon"], "rooms_total": int(rooms), "price_per_night_paise": None,
                "tier": stars_tier.get(stars) if stars else None, "accessible": h["tags"].get("wheelchair") == "yes",
                "transport_entity_id": station,
                "walk_to_transport_sec": int(round(walk_sec)) if walk_sec is not None else 900,
                "base_occupancy": float(self._cfg("hotel_base_occupancy", 0.45)),
                "rooms_source": rsrc, "rooms_confidence": rconf, "price_source": "unknown",
                "bed_capacity": cap["bed_capacity"], "bed_source": cap["bed_source"],
                "effective_guest_capacity": cap["effective_guest_capacity"],
                "guest_capacity_source": cap["guest_capacity_source"], "capacity_tags": cap["capacity_tags"],
                "occupancy_baseline_source": "simulated",
                "tier_source": "osm_stars" if stars else "unknown",
                "provenance": _prov("osm", f"{h['osm_type']}/{h['osm_id']}", "high", True, False),
            })

        # --- emergency ----------------------------------------------------------------------------------
        em_items = [{"key": f"osm:{x['osm_type']}/{x['osm_id']}", **x} for x in raw.emergency]
        ecap = self._cfg("emergency_capacity", {})
        emergency: list[str] = []
        esnap: dict[str, str] = {}
        for x in nearest_n(em_items, int(self._cfg("max_emergency", 10))):
            label = display(x["tags"], x["subtype"].replace("_", " ").title())
            s = snap_or_warn(label, x["lat"], x["lon"])
            if s is None:
                continue
            eid = add_node({
                "entity_id": _hid("n", f"emergency:{x['key']}"), "entity_type": "emergency_facility",
                "subtype": x["subtype"], "display_name": label, "lat": x["lat"], "lon": x["lon"],
                "nominal_capacity": float(ecap.get(x["subtype"], 60)), "capacity_source": "estimated_subtype_default",
                "capacity_confidence": "low", "parent_id": None,
                "provenance": _prov("osm", f"{x['osm_type']}/{x['osm_id']}", "high", True, False),
                "meta": {"beds": x["tags"].get("beds")},
            })
            emergency.append(eid)
            esnap[eid] = s[0]
            add_edge(eid, s[0], "connects_to", s[1] / walk, key="snap", distance=s[1],
                     prov=_prov("derived", "snap", "medium", True, True), travel_source="derived_geodesic_walk",
                     directionality="bidirectional")
        drive = float(self._cfg("drive_speed_mps", 8.0))
        if emergency:
            def nearest_em(road: str, k: int) -> list[tuple[float, str]]:
                dist, _ = walk_from(road)
                out = sorted((dist[esnap[e]], e) for e in emergency if esnap[e] in dist)
                return out[:k]
            for g in gates:
                for d, e in nearest_em(gate_snap[g][0], 1):
                    add_edge(g, e, "evacuates_to", d / drive, key="evac", distance=d, coeff=0.3,
                             prov=_prov("derived", "shortest_road_path", "low", True, True),
                             travel_source="derived_shortest_path_drive")
                    add_edge(gate_snap[g][0], e, "evacuates_to", d / drive, key="evac", distance=d, coeff=0.2,
                             prov=_prov("derived", "shortest_road_path", "low", True, True),
                             travel_source="derived_shortest_path_drive")
            for d, e in nearest_em(gate_snap[gates[0]][0], 2):
                add_edge(venue_id, e, "evacuates_to", d / drive, key="evac", distance=d, coeff=0.15,
                         prov=_prov("derived", "shortest_road_path", "low", True, True),
                         travel_source="derived_shortest_path_drive")
        else:
            warnings.append("No hospital, police or fire station found in the footprint; emergency coupling is not modelled.")

        # --- venue precinct zone ------------------------------------------------------------------------
        if ring:
            plat = _r(sum(p[0] for p in ring) / len(ring))
            plon = _r(sum(p[1] for p in ring) / len(ring))
        else:
            plat, plon = venue["lat"], venue["lon"]
        zone_id = add_node({
            "entity_id": _hid("n", f"precinct:{venue['venue_id']}"), "entity_type": "zone", "subtype": "venue_precinct",
            "display_name": f"{venue['display_name']} precinct"[:120], "lat": plat, "lon": plon,
            "nominal_capacity": float(max(500.0, round(vcap * float(self._cfg("precinct_capacity_ratio", 0.25)), 0))),
            "capacity_source": "derived_venue_capacity", "capacity_confidence": "low", "parent_id": venue_id,
            "provenance": _prov("derived", None, "low", True, True), "meta": {},
        })
        for g in gates:
            dz = haversine_m(nodes[g]["lat"], nodes[g]["lon"], plat, plon)
            add_edge(g, zone_id, "serves", max(30.0, dz / walk), key="precinct", distance=dz, coeff=0.3,
                     prov=_prov("derived", None, "low", True, True), travel_source="derived_geodesic_walk")
        for a in transit_nodes:
            if a in nodes and haversine_m(nodes[a]["lat"], nodes[a]["lon"], plat, plon) <= 1000:
                dz = haversine_m(nodes[a]["lat"], nodes[a]["lon"], plat, plon)
                add_edge(a, zone_id, "last_mile_to", dz / walk, key="precinct", distance=dz, coeff=0.2,
                         prov=_prov("derived", None, "low", True, True), travel_source="derived_geodesic_walk")

        # --- substitutes (relief options for interventions and closures) ---------------------------------
        def add_substitutes(ids: list[str], same, k: int, reach_m: float) -> None:
            for a in ids:
                cands = sorted((haversine_m(nodes[a]["lat"], nodes[a]["lon"], nodes[b]["lat"], nodes[b]["lon"]), b)
                               for b in ids if b != a and same(a, b))
                for d, b in [c for c in cands if c[0] <= reach_m][:k]:
                    add_edge(a, b, "substitutes_for", d * 1.3 / walk, key="sub", distance=d * 1.3,
                             subst=round(max(0.3, 0.8 - d / reach_m * 0.5), 3),
                             prov=_prov("derived", None, "low", True, True), travel_source="derived_geodesic_walk_x1.3")

        def group(n: dict) -> str:
            return "rail" if n.get("subtype") in RAIL_SUBTYPES else "bus"
        add_substitutes(transit_nodes, lambda a, b: group(nodes[a]) == group(nodes[b]), 2, 2000.0)
        add_substitutes([a for a in access if nodes[a]["entity_type"] == "parking"], lambda a, b: True, 2, 1500.0)
        add_substitutes(sorted(p["cluster_entity_id"] for p in properties), lambda a, b: True, 2, 3000.0)

        # --- assemble -----------------------------------------------------------------------------------
        max_nodes = int(self._cfg("max_nodes", 2500))
        if len(nodes) > max_nodes:
            raise BlueprintError("NETWORK_TOO_LARGE", f"The footprint produced {len(nodes)} entities (limit {max_nodes}); "
                                 "reduce the radius.", {"nodes": len(nodes), "limit": max_nodes})
        node_list = [nodes[k] for k in sorted(nodes)]
        edge_list = [edges[k] for k in sorted(edges)]
        properties.sort(key=lambda p: p["property_id"])
        lats = [n["lat"] for n in node_list] + [fp.bounds["min_lat"], fp.bounds["max_lat"]]
        lons = [n["lon"] for n in node_list] + [fp.bounds["min_lon"], fp.bounds["max_lon"]]
        bounds = {"min_lat": _r(min(lats)), "max_lat": _r(max(lats)), "min_lon": _r(min(lons)), "max_lon": _r(max(lons))}
        canonical = {
            "venue": {k: venue.get(k) for k in ("venue_id", "display_name", "lat", "lon", "source", "source_id")},
            "radius_m": fp.radius_m, "nodes": node_list, "edges": edge_list, "properties": properties,
            "venue_geometry": ring, "params": {k: self.c[k] for k in sorted(self.c)}, "venue_capacity": venue_capacity,
            "max_gates": max_gates,
        }
        graph_hash = hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":"),
                                               default=str).encode("utf-8")).hexdigest()
        counts = {t: sum(1 for n in node_list if n["entity_type"] == t) for t in
                  ("road", "transport_node", "parking", "hotel", "emergency_facility", "gate", "venue", "zone")}
        summary = {
            "road_nodes": counts["road"],
            "road_edges": sum(1 for e in edge_list if e["edge_type"] == "adjacent_to"
                              and nodes[e["src_entity_id"]]["entity_type"] == "road"
                              and nodes[e["dst_entity_id"]]["entity_type"] == "road"),
            "transport_nodes": counts["transport_node"],
            "hotels": counts["hotel"], "parking": counts["parking"], "emergency": counts["emergency_facility"],
            "access_points": counts["gate"], "zones": counts["zone"], "venues": counts["venue"],
            "total_nodes": len(node_list), "total_edges": len(edge_list),
            "transit_by_subtype": {s: sum(1 for n in node_list if n.get("subtype") == s)
                                   for s in RAIL_SUBTYPES + BUS_SUBTYPES},
            "gate_inference": gate_mode,
        }
        prov_summary: dict[str, dict[str, int]] = {"capacity_source": {}, "travel_time_source": {}, "node_source": {}}
        for n in node_list:
            prov_summary["capacity_source"][n["capacity_source"]] = prov_summary["capacity_source"].get(n["capacity_source"], 0) + 1
            prov_summary["node_source"][n["provenance"]["source"]] = prov_summary["node_source"].get(n["provenance"]["source"], 0) + 1
        for e in edge_list:
            prov_summary["travel_time_source"][e["travel_time_source"]] = prov_summary["travel_time_source"].get(e["travel_time_source"], 0) + 1
        for k in prov_summary:
            prov_summary[k] = dict(sorted(prov_summary[k].items()))
        return {
            "blueprint_id": "bp_" + graph_hash[:12],
            "graph_hash": graph_hash,
            "venue": venue,
            "venue_entity_id": venue_id,
            "venue_geometry": [[p[1], p[0]] for p in ring] if ring else None,
            "footprint": fp.to_dict(),
            "nodes": node_list,
            "edges": edge_list,
            "zones": [zone_id],
            "properties": properties,
            "segments": self.segments,
            "bounds": bounds,
            "summary": summary,
            "warnings": _collapse(warnings),
            "provenance_summary": prov_summary,
            "source_summary": {"raw": raw.counts(), "skipped": raw.skipped},
            "metadata": {
                "data_source": data_source, "provider": raw.meta.get("provider"), "endpoint": raw.meta.get("endpoint"),
                "osm_base_timestamp": raw.meta.get("osm_base_timestamp"), "attribution": ATTRIBUTION,
                "query_sha1": raw.meta.get("query_sha1"), "fetch_ms": raw.meta.get("fetch_ms"),
                "parameters": {"radius_m": fp.radius_m, "venue_capacity": venue_capacity, "max_gates": max_gates,
                               "road_classes": raw.meta.get("road_classes")},
            },
            "persistable": bool(venue.get("persistable", True)),
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }


def _collapse(warnings: list[str]) -> list[str]:
    """Identical warnings (e.g. several unnamed car parks off the road network) once, with a count."""
    counts: dict[str, int] = {}
    for w in warnings:
        counts[w] = counts.get(w, 0) + 1
    return [w if n == 1 else f"{w} (x{n})" for w, n in counts.items()]
