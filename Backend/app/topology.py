"""The demo event graph — 00_SHARED_CONTRACT.md §5 naming registry.

Built in code rather than hand-written JSON so the frozen IDs, the demo cascade
chain and the gate_5 saturation trap are all visible in one place and cannot
drift apart. 67 entities, ~130 edges.

Two structural facts the demo depends on, asserted at the bottom of this file:
  * the chain `metro_b -> gate_3 -> road_4 -> emergency_north` exists
  * `metro_c` feeds `gate_5` hard enough that rerouting to it saturates gate_5
"""
from __future__ import annotations

from typing import Any

# Venue centre; every other coordinate is offset from it so the map reads as one site.
VENUE_LAT, VENUE_LON = 19.0760, 72.8777


def _e(
    entity_id: str,
    entity_type: str,
    display_name: str,
    dlat: float,
    dlon: float,
    nominal_capacity: float,
    parent_id: str | None = None,
    **meta: Any,
) -> dict[str, Any]:
    return {
        "entity_id": entity_id,
        "entity_type": entity_type,
        "display_name": display_name,
        "lat": round(VENUE_LAT + dlat, 6),
        "lon": round(VENUE_LON + dlon, 6),
        "nominal_capacity": float(nominal_capacity),
        "parent_id": parent_id,
        "meta": meta,
    }


def build_entities() -> list[dict[str, Any]]:
    n: list[dict[str, Any]] = []

    # --- venue ---------------------------------------------------------
    n.append(_e("stadium_main", "venue", "Main Stadium", 0.0, 0.0, 70000, tiers=3))
    # Second venue: a convention centre hosting a multi-day expo whose egress
    # overlaps the stadium's arrival wave (concurrent-event pressure).
    n.append(_e("convention_centre", "venue", "City Convention Centre", 0.0185, -0.0105, 15000, halls=4))

    # --- gates (frozen 1..6) -------------------------------------------
    # gate_3 sits on the demo chain; gate_5 is deliberately the smallest, so a
    # naive reroute onto metro_c pushes it past 1.0 (00 §5 "unstable target").
    gates = [
        ("gate_1", "Gate 1", 0.0045, -0.0040, 2800),
        ("gate_2", "Gate 2", 0.0050, 0.0010, 2800),
        ("gate_3", "Gate 3", 0.0030, 0.0055, 2200),
        ("gate_4", "Gate 4", -0.0025, 0.0060, 2500),
        ("gate_5", "Gate 5", -0.0050, 0.0005, 1500),
        ("gate_6", "Gate 6", -0.0040, -0.0045, 2600),
    ]
    for gid, name, dlat, dlon, cap in gates:
        n.append(_e(gid, "gate", name, dlat, dlon, cap, parent_id="stadium_main"))

    # --- transport nodes ------------------------------------------------
    transport_nodes = [
        ("metro_a", "Metro A Station", 0.0120, -0.0130, 3800, ["blue"], 2),
        ("metro_b", "Metro B Station", 0.0095, 0.0140, 4200, ["blue"], 2),
        ("metro_c", "Metro C Station", -0.0110, 0.0090, 3600, ["red"], 2),
        ("metro_d", "Metro D Station", -0.0150, -0.0080, 3100, ["red"], 1),
        ("metro_e", "Metro E Station", 0.0180, 0.0030, 2900, ["green"], 1),
        ("bus_hub_north", "North Bus Hub", 0.0160, -0.0055, 2400, [], 0),
        ("bus_hub_south", "South Bus Hub", -0.0175, 0.0025, 2200, [], 0),
        ("shuttle_hub_east", "East Shuttle Hub", 0.0015, 0.0175, 1800, [], 0),
        ("shuttle_hub_west", "West Shuttle Hub", -0.0010, -0.0185, 1800, [], 0),
    ]
    for tid, name, dlat, dlon, cap, lines, platforms in transport_nodes:
        n.append(_e(tid, "transport_node", name, dlat, dlon, cap, lines=lines, platforms=platforms))

    # --- transport routes (capacity is people/hour) ----------------------
    n.append(_e("line_blue", "transport_route", "Blue Line", 0.0110, 0.0005, 24000, colour="blue"))
    n.append(_e("line_red", "transport_route", "Red Line", -0.0130, 0.0055, 21000, colour="red"))
    n.append(_e("line_green", "transport_route", "Green Line", 0.0170, 0.0100, 15000, colour="green"))

    # --- roads: capacity is peak people-equivalent load on the segment. The
    # funnel narrows deliberately (station 4200 -> gate 2200 -> road 900 ->
    # emergency post 140); a flat capacity profile stops any cascade from
    # clearing the propagation threshold, which is physically wrong and kills
    # the demo chain.
    roads = [
        ("road_1", "Arterial 1", 0.0075, -0.0090, 1100),
        ("road_2", "Arterial 2", 0.0085, 0.0055, 1000),
        ("road_3", "Arterial 3", 0.0020, 0.0105, 950),
        ("road_4", "Arterial 4", -0.0015, 0.0110, 900),
        ("road_5", "Arterial 5", -0.0080, 0.0045, 1000),
        ("road_6", "Arterial 6", -0.0075, -0.0075, 1050),
        ("road_7", "Arterial 7", 0.0135, -0.0035, 850),
        ("road_8", "Arterial 8", 0.0125, 0.0085, 900),
        ("road_9", "Arterial 9", -0.0135, 0.0110, 780),
        ("road_10", "Arterial 10", -0.0140, -0.0040, 820),
        ("road_11", "Arterial 11", 0.0045, 0.0160, 740),
        ("road_12", "Arterial 12", -0.0045, -0.0165, 740),
        ("road_13", "Arterial 13", 0.0195, -0.0015, 700),
        ("road_14", "Arterial 14", -0.0195, 0.0060, 700),
        ("road_15", "Arterial 15", 0.0100, 0.0185, 660),
        ("road_16", "Arterial 16", -0.0100, -0.0195, 660),
        ("road_17", "Arterial 17", 0.0060, -0.0155, 780),
        ("road_18", "Arterial 18", -0.0060, 0.0165, 780),
    ]
    for rid, name, dlat, dlon, cap in roads:
        n.append(_e(rid, "road", name, dlat, dlon, cap, lanes=2))

    # --- zones (capacity is people) --------------------------------------
    zones = [
        ("zone_north", "North Zone", 0.0065, 0.0000, 14000),
        ("zone_south", "South Zone", -0.0065, 0.0000, 13000),
        ("zone_east", "East Zone", 0.0000, 0.0075, 11000),
        ("zone_west", "West Zone", 0.0000, -0.0075, 11500),
        ("zone_core", "Core Zone", 0.0008, 0.0008, 16000),
        ("zone_concourse_north", "North Concourse", 0.0038, 0.0018, 6000),
        ("zone_concourse_south", "South Concourse", -0.0038, -0.0018, 6000),
        ("zone_fanpark", "Fan Park", 0.0090, -0.0075, 9000),
        ("zone_plaza_east", "East Plaza", 0.0010, 0.0125, 5000),
        ("zone_plaza_west", "West Plaza", -0.0010, -0.0130, 5000),
    ]
    for zid, name, dlat, dlon, cap in zones:
        n.append(_e(zid, "zone", name, dlat, dlon, cap))

    # --- hotels (capacity is rooms) ---------------------------------------
    hotels = [
        ("hotel_north_cluster", "North Hotel Cluster", 0.0145, 0.0060, 1400),
        ("hotel_core_cluster", "Core Hotel Cluster", 0.0025, -0.0025, 1800),
        ("hotel_east_cluster", "East Hotel Cluster", -0.0030, 0.0150, 1100),
        ("hotel_south_cluster", "South Hotel Cluster", -0.0160, -0.0015, 950),
        ("hotel_west_cluster", "West Hotel Cluster", 0.0030, -0.0160, 1000),
        ("hotel_airport_cluster", "Airport Hotel Cluster", 0.0215, 0.0135, 1600),
    ]
    for hid, name, dlat, dlon, cap in hotels:
        n.append(_e(hid, "hotel", name, dlat, dlon, cap))

    # --- parking (capacity is vehicles) ------------------------------------
    parkings = [
        ("parking_p1", "Parking P1", 0.0072, -0.0058, 2200),
        ("parking_p2", "Parking P2", 0.0068, 0.0072, 1900),
        ("parking_p3", "Parking P3", -0.0070, 0.0068, 1700),
        ("parking_p4", "Parking P4", -0.0072, -0.0062, 2000),
        ("parking_p5", "Parking P5", 0.0115, -0.0100, 1500),
        ("parking_p6", "Parking P6", 0.0110, 0.0115, 1400),
        ("parking_p7", "Parking P7", -0.0118, 0.0095, 1300),
        ("parking_p8", "Parking P8", -0.0112, -0.0105, 1350),
        ("parking_p9", "Parking P9", 0.0165, 0.0140, 1100),
        ("parking_p10", "Parking P10", -0.0168, -0.0135, 1050),
    ]
    for pid, name, dlat, dlon, cap in parkings:
        n.append(_e(pid, "parking", name, dlat, dlon, cap))

    # --- emergency ----------------------------------------------------------
    n.append(_e("emergency_north", "emergency_facility", "North Emergency Post", 0.0055, 0.0125, 140, bays=6))
    n.append(_e("emergency_south", "emergency_facility", "South Emergency Post", -0.0058, -0.0120, 130, bays=5))
    n.append(_e("emergency_core", "emergency_facility", "Core Emergency Post", -0.0005, 0.0022, 110, bays=4))

    return n


def _edge(
    src: str, dst: str, edge_type: str, coeff: float, travel_sec: int, subst: float = 0.0
) -> dict[str, Any]:
    return {
        "edge_id": f"{src}__{dst}__{edge_type}",
        "src_entity_id": src,
        "dst_entity_id": dst,
        "edge_type": edge_type,
        "transfer_coefficient": coeff,
        "travel_time_sec": travel_sec,
        "substitutability": subst,
    }


def build_edges() -> list[dict[str, Any]]:
    e: list[dict[str, Any]] = []

    # --- lines feed their stations ---------------------------------------
    e += [
        _edge("line_blue", "metro_a", "feeds", 0.40, 120),
        _edge("line_blue", "metro_b", "feeds", 0.55, 120),
        _edge("line_red", "metro_c", "feeds", 0.48, 120),
        _edge("line_red", "metro_d", "feeds", 0.42, 120),
        _edge("line_green", "metro_e", "feeds", 0.50, 120),
    ]

    # --- stations feed gates ---------------------------------------------
    # metro_b -> gate_3 is step 1 of the demo chain; the coefficient is the
    # highest on the map because that is the pressure the demo shows.
    e += [
        _edge("metro_b", "gate_3", "feeds", 0.62, 420),
        _edge("metro_b", "gate_4", "feeds", 0.24, 540),
        _edge("metro_a", "gate_1", "feeds", 0.55, 480),
        _edge("metro_a", "gate_2", "feeds", 0.30, 520),
        _edge("metro_c", "gate_5", "feeds", 0.68, 400),   # the saturation trap
        _edge("metro_c", "gate_6", "feeds", 0.26, 500),
        _edge("metro_d", "gate_6", "feeds", 0.45, 560),
        _edge("metro_e", "gate_1", "feeds", 0.32, 660),
        _edge("bus_hub_north", "gate_2", "feeds", 0.40, 480),
        _edge("bus_hub_south", "gate_5", "feeds", 0.34, 500),
        _edge("shuttle_hub_east", "gate_4", "feeds", 0.38, 360),
        _edge("shuttle_hub_west", "gate_6", "feeds", 0.38, 360),
    ]

    # --- gates feed the venue --------------------------------------------
    for gid in ("gate_1", "gate_2", "gate_3", "gate_4", "gate_5", "gate_6"):
        e.append(_edge(gid, "stadium_main", "feeds", 0.90, 180))

    # --- gate spill onto adjacent roads (chain step 2) --------------------
    e += [
        _edge("gate_3", "road_4", "adjacent_to", 0.62, 300),
        _edge("gate_3", "road_3", "adjacent_to", 0.30, 300),
        _edge("gate_1", "road_1", "adjacent_to", 0.32, 300),
        _edge("gate_2", "road_2", "adjacent_to", 0.30, 300),
        _edge("gate_4", "road_3", "adjacent_to", 0.28, 320),
        _edge("gate_5", "road_5", "adjacent_to", 0.34, 300),
        _edge("gate_6", "road_6", "adjacent_to", 0.30, 320),
    ]

    # --- road network -----------------------------------------------------
    road_links = [
        ("road_1", "road_7"), ("road_1", "road_17"), ("road_2", "road_8"),
        ("road_2", "road_3"), ("road_3", "road_4"), ("road_3", "road_11"),
        ("road_4", "road_18"), ("road_4", "road_5"), ("road_5", "road_9"),
        ("road_5", "road_6"), ("road_6", "road_10"), ("road_6", "road_12"),
        ("road_7", "road_13"), ("road_8", "road_15"), ("road_9", "road_14"),
        ("road_10", "road_14"), ("road_11", "road_15"), ("road_12", "road_16"),
        ("road_13", "road_17"), ("road_18", "road_11"),
    ]
    for a, b in road_links:
        e.append(_edge(a, b, "adjacent_to", 0.22, 240))

    # --- convention centre access (second venue) ---------------------------
    e += [
        _edge("metro_a", "convention_centre", "feeds", 0.45, 360),
        _edge("metro_e", "convention_centre", "feeds", 0.35, 420),
        _edge("bus_hub_north", "convention_centre", "feeds", 0.40, 300),
        _edge("convention_centre", "road_7", "adjacent_to", 0.30, 240),
        _edge("convention_centre", "road_13", "adjacent_to", 0.25, 240),
        _edge("convention_centre", "emergency_north", "evacuates_to", 0.10, 420),
        # fan park is itself an event venue (the Fan Festival); metro_a is its
        # nearest rail access alongside the north bus hub.
        _edge("metro_a", "zone_fanpark", "last_mile_to", 0.35, 540),
    ]

    # --- emergency egress (chain step 3) ----------------------------------
    e += [
        _edge("road_4", "emergency_north", "evacuates_to", 0.30, 300),
        _edge("road_3", "emergency_north", "evacuates_to", 0.22, 360),
        _edge("road_6", "emergency_south", "evacuates_to", 0.26, 320),
        _edge("road_5", "emergency_south", "evacuates_to", 0.20, 340),
        _edge("stadium_main", "emergency_core", "evacuates_to", 0.15, 240),
        # Deliberately NO direct gate_3 -> emergency_north edge: it would short-
        # circuit the demo chain, and the point of the chain is that the
        # emergency post is reached through the road, two hops downstream.
    ]

    # --- parking serves gates/venue ---------------------------------------
    parking_links = [
        ("parking_p1", "gate_1"), ("parking_p2", "gate_2"), ("parking_p3", "gate_4"),
        ("parking_p4", "gate_6"), ("parking_p5", "gate_1"), ("parking_p6", "gate_3"),
        ("parking_p7", "gate_5"), ("parking_p8", "gate_6"), ("parking_p9", "gate_2"),
        ("parking_p10", "gate_5"),
    ]
    for pid, gid in parking_links:
        e.append(_edge(pid, gid, "serves", 0.35, 420))

    # --- parking substitutes (relief options for the optimiser) ------------
    parking_subs = [
        ("parking_p1", "parking_p5"), ("parking_p2", "parking_p6"),
        ("parking_p3", "parking_p7"), ("parking_p4", "parking_p8"),
        ("parking_p6", "parking_p9"), ("parking_p7", "parking_p10"),
    ]
    for a, b in parking_subs:
        e.append(_edge(a, b, "substitutes_for", 0.0, 300, subst=0.7))

    # --- hotels: last mile to transport -----------------------------------
    hotel_links = [
        ("hotel_north_cluster", "metro_b", 720), ("hotel_core_cluster", "metro_c", 480),
        ("hotel_east_cluster", "shuttle_hub_east", 540), ("hotel_south_cluster", "metro_d", 600),
        ("hotel_west_cluster", "shuttle_hub_west", 560), ("hotel_airport_cluster", "metro_e", 900),
    ]
    for hid, tid, secs in hotel_links:
        e.append(_edge(hid, tid, "last_mile_to", 0.40, secs))

    # --- hotel substitution (accommodation_rebalance candidates) -----------
    hotel_subs = [
        ("hotel_core_cluster", "hotel_north_cluster", 0.65),
        ("hotel_core_cluster", "hotel_east_cluster", 0.55),
        ("hotel_north_cluster", "hotel_airport_cluster", 0.45),
        ("hotel_east_cluster", "hotel_south_cluster", 0.40),
        ("hotel_west_cluster", "hotel_core_cluster", 0.50),
    ]
    for a, b, s in hotel_subs:
        e.append(_edge(a, b, "substitutes_for", 0.0, 600, subst=s))

    # --- station substitution (reroute_transport candidates) ---------------
    # metro_b -> metro_c is the reroute the demo proposes; it is also the one
    # that saturates gate_5, which is why its certificate comes back UNSTABLE.
    station_subs = [
        ("metro_b", "metro_c", 0.72), ("metro_b", "metro_e", 0.50),
        ("metro_a", "metro_e", 0.55), ("metro_c", "metro_d", 0.60),
        ("metro_d", "bus_hub_south", 0.40), ("metro_a", "bus_hub_north", 0.45),
    ]
    for a, b, s in station_subs:
        e.append(_edge(a, b, "substitutes_for", 0.0, 480, subst=s))

    # --- last mile: stations/hubs to zones ---------------------------------
    last_mile = [
        ("metro_b", "zone_north", 480), ("metro_b", "zone_east", 540),
        ("metro_a", "zone_west", 500), ("metro_c", "zone_south", 460),
        ("metro_d", "zone_south", 620), ("metro_e", "zone_north", 700),
        ("shuttle_hub_east", "zone_plaza_east", 300),
        ("shuttle_hub_west", "zone_plaza_west", 300),
        ("bus_hub_north", "zone_fanpark", 420),
        ("bus_hub_south", "zone_south", 400),
    ]
    for src, zid, secs in last_mile:
        e.append(_edge(src, zid, "last_mile_to", 0.35, secs))

    # --- zone adjacency (the load_variance surface) -------------------------
    zone_links = [
        ("zone_north", "zone_core"), ("zone_south", "zone_core"),
        ("zone_east", "zone_core"), ("zone_west", "zone_core"),
        ("zone_north", "zone_concourse_north"), ("zone_south", "zone_concourse_south"),
        ("zone_east", "zone_plaza_east"), ("zone_west", "zone_plaza_west"),
        ("zone_north", "zone_fanpark"), ("zone_concourse_north", "zone_core"),
        ("zone_concourse_south", "zone_core"),
    ]
    for a, b in zone_links:
        e.append(_edge(a, b, "adjacent_to", 0.25, 180))

    # --- zone substitution (zone_incentive candidates) ----------------------
    zone_subs = [
        ("zone_core", "zone_north", 0.70), ("zone_core", "zone_west", 0.62),
        ("zone_east", "zone_north", 0.58), ("zone_south", "zone_west", 0.55),
        ("zone_concourse_north", "zone_fanpark", 0.50),
    ]
    for a, b, s in zone_subs:
        e.append(_edge(a, b, "substitutes_for", 0.0, 420, subst=s))

    # --- gates serve zones (gate_redistribution candidates) -----------------
    gate_zone = [
        ("gate_1", "zone_west"), ("gate_2", "zone_north"), ("gate_3", "zone_east"),
        ("gate_4", "zone_east"), ("gate_5", "zone_south"), ("gate_6", "zone_west"),
    ]
    for gid, zid in gate_zone:
        e.append(_edge(gid, zid, "serves", 0.30, 240))

    return e


# 00 §1.8 — exactly five segments. Elasticities are stated assumptions, not
# measurements; the certificate's compliance sweep is what covers that risk.
SEGMENTS: list[dict[str, Any]] = [
    {
        "segment_id": "price_sensitive", "display_name": "Price sensitive", "share": 0.28,
        "price_elasticity": 0.72, "time_elasticity": 0.21,
        "accessibility_constrained": False, "compliance_base_rate": 0.55,
    },
    {
        "segment_id": "time_sensitive", "display_name": "Time sensitive", "share": 0.24,
        "price_elasticity": 0.18, "time_elasticity": 0.81,
        "accessibility_constrained": False, "compliance_base_rate": 0.48,
    },
    {
        "segment_id": "accessibility_constrained", "display_name": "Accessibility constrained",
        "share": 0.09, "price_elasticity": 0.25, "time_elasticity": 0.30,
        "accessibility_constrained": True, "compliance_base_rate": 0.35,
    },
    {
        "segment_id": "group", "display_name": "Group", "share": 0.27,
        "price_elasticity": 0.61, "time_elasticity": 0.34,
        "accessibility_constrained": False, "compliance_base_rate": 0.42,
    },
    {
        "segment_id": "premium", "display_name": "Premium", "share": 0.12,
        "price_elasticity": 0.09, "time_elasticity": 0.44,
        "accessibility_constrained": False, "compliance_base_rate": 0.30,
    },
]


def build_bounds(entities: list[dict[str, Any]]) -> dict[str, float]:
    lats = [e["lat"] for e in entities]
    lons = [e["lon"] for e in entities]
    pad = 0.004
    return {
        "min_lat": round(min(lats) - pad, 6),
        "max_lat": round(max(lats) + pad, 6),
        "min_lon": round(min(lons) - pad, 6),
        "max_lon": round(max(lons) + pad, 6),
    }


def build_topology() -> dict[str, Any]:
    entities = build_entities()
    edges = build_edges()
    return {
        "nodes": entities,
        "edges": edges,
        "segments": SEGMENTS,
        "bounds": build_bounds(entities),
    }


# --- structural guarantees the demo depends on ---------------------------
DEMO_CASCADE_CHAIN = ["metro_b", "gate_3", "road_4", "emergency_north"]
DEMO_UNSTABLE_TARGET = "gate_5"


class TopologyIntegrityError(RuntimeError):
    """A structural guarantee the demo depends on does not hold."""


def verify(topology: dict[str, Any]) -> None:
    """Fail loudly at startup rather than quietly at demo time.

    Plain `raise`, not `assert` (M8): `assert` is compiled out entirely under
    `python -O` / `PYTHONOPTIMIZE`, which would make every guarantee here a
    silent no-op — exactly the "fails quietly at demo time" this function
    exists to prevent.
    """
    ids = {n["entity_id"] for n in topology["nodes"]}
    if len(topology["nodes"]) < 60:
        raise TopologyIntegrityError(f"01 §8 requires >=60 nodes, got {len(topology['nodes'])}")

    missing = [e for e in DEMO_CASCADE_CHAIN + [DEMO_UNSTABLE_TARGET] if e not in ids]
    if missing:
        raise TopologyIntegrityError(f"demo entities missing from topology: {missing}")

    edge_ids = {e["edge_id"] for e in topology["edges"]}
    for src, dst in zip(DEMO_CASCADE_CHAIN, DEMO_CASCADE_CHAIN[1:]):
        if not any(
            e["src_entity_id"] == src and e["dst_entity_id"] == dst for e in topology["edges"]
        ):
            raise TopologyIntegrityError(f"demo cascade chain broken: no edge {src} -> {dst}")
    if "metro_c__gate_5__feeds" not in edge_ids:
        raise TopologyIntegrityError("gate_5 saturation trap is missing")

    for e in topology["edges"]:
        if e["src_entity_id"] not in ids or e["dst_entity_id"] not in ids:
            raise TopologyIntegrityError(f"dangling edge {e['edge_id']}")

    shares = sum(s["share"] for s in topology["segments"])
    if abs(shares - 1.0) >= 1e-6:
        raise TopologyIntegrityError(f"segment shares must sum to 1.0, got {shares}")
