"""Static accommodation catalogue — the individual properties behind each hotel cluster.

The six `hotel_*_cluster` graph entities (00 §5) stay the map-level, frozen IDs;
each one is the aggregate of the properties listed here, so a cluster's
`nominal_capacity` is always exactly the sum of its properties' rooms (asserted
in `verify_catalogue`). Prices are integer paise per night (00 §0).

Everything that varies per property but is not hand-authored (base occupancy,
walking time to transport, coordinate offsets) is derived from `stable_unit`,
so the catalogue is identical on every run with the same seed.
"""
from __future__ import annotations

from typing import Any

from .ml_reference.common import stable_unit

TIERS = ("budget", "midscale", "upscale", "luxury")

# (property_id, name, cluster_id, rooms, price_rupees, tier, accessible)
_PROPERTIES: list[tuple[str, str, str, int, int, str, bool]] = [
    ("htl_northgate_grand", "Northgate Grand", "hotel_north_cluster", 320, 14500, "luxury", True),
    ("htl_metro_b_residency", "Metro B Residency", "hotel_north_cluster", 420, 6200, "midscale", True),
    ("htl_stadium_view_inn", "Stadium View Inn", "hotel_north_cluster", 380, 3400, "budget", False),
    ("htl_north_park_suites", "North Park Suites", "hotel_north_cluster", 280, 9800, "upscale", True),
    ("htl_core_plaza", "Core Plaza Hotel", "hotel_core_cluster", 500, 11000, "upscale", True),
    ("htl_central_budget", "Central Budget Stay", "hotel_core_cluster", 450, 2900, "budget", False),
    ("htl_arena_business", "Arena Business Hotel", "hotel_core_cluster", 480, 5800, "midscale", True),
    ("htl_continental", "The Continental", "hotel_core_cluster", 370, 16500, "luxury", True),
    ("htl_east_bay", "East Bay Hotel", "hotel_east_cluster", 400, 5200, "midscale", True),
    ("htl_harbour_lodge", "Harbour Lodge", "hotel_east_cluster", 350, 2600, "budget", False),
    ("htl_eastside_suites", "Eastside Suites", "hotel_east_cluster", 350, 8900, "upscale", True),
    ("htl_south_station_inn", "South Station Inn", "hotel_south_cluster", 330, 2400, "budget", False),
    ("htl_riverside", "Riverside Hotel", "hotel_south_cluster", 360, 4800, "midscale", False),
    ("htl_southgate_apts", "Southgate Apartments", "hotel_south_cluster", 260, 5500, "midscale", True),
    ("htl_westend", "Westend Hotel", "hotel_west_cluster", 380, 4600, "midscale", True),
    ("htl_west_lake_resort", "West Lake Resort", "hotel_west_cluster", 320, 10200, "upscale", True),
    ("htl_backpackers_west", "Backpackers West", "hotel_west_cluster", 300, 1800, "budget", False),
    ("htl_airport_grand", "Airport Grand", "hotel_airport_cluster", 520, 9500, "upscale", True),
    ("htl_runway_inn", "Runway Inn", "hotel_airport_cluster", 560, 3100, "budget", True),
    ("htl_skylounge_suites", "SkyLounge Suites", "hotel_airport_cluster", 520, 13800, "luxury", True),
]

_CLUSTER_ZONE = {
    "hotel_north_cluster": "North",
    "hotel_core_cluster": "Core",
    "hotel_east_cluster": "East",
    "hotel_south_cluster": "South",
    "hotel_west_cluster": "West",
    "hotel_airport_cluster": "Airport",
}


def build_properties(nodes: list[dict[str, Any]], edges: list[dict[str, Any]], seed: int = 42) -> list[dict[str, Any]]:
    by_id = {n["entity_id"]: n for n in nodes}
    station_of: dict[str, tuple[str, int]] = {}
    for e in edges:
        if e["edge_type"] == "last_mile_to" and by_id.get(e["src_entity_id"], {}).get("entity_type") == "hotel":
            station_of.setdefault(e["src_entity_id"], (e["dst_entity_id"], int(e["travel_time_sec"])))

    out: list[dict[str, Any]] = []
    for pid, name, cluster, rooms, rupees, tier, accessible in _PROPERTIES:
        if cluster not in by_id:
            continue          # a topology without the demo hotel clusters (file / generated worlds)
        c = by_id[cluster]
        station, cluster_walk = station_of.get(cluster, (None, 600))
        u = stable_unit(seed, "property", pid)
        out.append(
            {
                "property_id": pid,
                "name": name,
                "cluster_entity_id": cluster,
                "zone": _CLUSTER_ZONE.get(cluster, "City"),
                "lat": round(c["lat"] + (stable_unit(seed, "plat", pid) - 0.5) * 0.004, 6),
                "lon": round(c["lon"] + (stable_unit(seed, "plon", pid) - 0.5) * 0.004, 6),
                "rooms_total": rooms,
                "price_per_night_paise": rupees * 100,
                "tier": tier,
                "accessible": accessible,
                "transport_entity_id": station,
                # Walking time from the property to its transport node, spread
                # around the cluster's last-mile edge time.
                "walk_to_transport_sec": int(cluster_walk * (0.6 + 0.8 * u)),
                # Occupancy from non-event guests, before any event booking.
                "base_occupancy": round(0.30 + 0.18 * stable_unit(seed, "baseocc", pid), 3),
            }
        )
    covered = {p["cluster_entity_id"] for p in out}
    out.extend(properties_from_hotel_nodes([n for n in nodes if n["entity_id"] not in covered], edges))
    return out


def properties_from_hotel_nodes(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One property per hotel entity that has no catalogue entry: rooms = the entity's
    capacity, price and tier unknown (null) — nothing is fabricated."""
    station_of: dict[str, tuple[str, int]] = {}
    for e in sorted(edges, key=lambda e: e["edge_id"]):
        if e["edge_type"] == "last_mile_to":
            station_of.setdefault(e["src_entity_id"], (e["dst_entity_id"], int(e["travel_time_sec"])))
    out = []
    for n in sorted((n for n in nodes if n["entity_type"] == "hotel"), key=lambda n: n["entity_id"]):
        station, walk = station_of.get(n["entity_id"], (None, 900))
        out.append({
            "property_id": "p_" + n["entity_id"], "name": n["display_name"], "cluster_entity_id": n["entity_id"],
            "zone": "Within footprint", "lat": n["lat"], "lon": n["lon"],
            "rooms_total": int(round(n["nominal_capacity"])), "price_per_night_paise": None, "tier": None,
            "accessible": False, "transport_entity_id": station, "walk_to_transport_sec": walk,
            "base_occupancy": 0.45, "rooms_source": n.get("capacity_source", "entity_capacity"),
            "rooms_confidence": n.get("capacity_confidence", "low"), "price_source": "unknown",
        })
    return out


def verify_catalogue(properties: list[dict[str, Any]], nodes: list[dict[str, Any]]) -> None:
    rooms: dict[str, int] = {}
    for p in properties:
        rooms[p["cluster_entity_id"]] = rooms.get(p["cluster_entity_id"], 0) + p["rooms_total"]
    for n in nodes:
        if n["entity_type"] != "hotel":
            continue
        if rooms.get(n["entity_id"], 0) != int(n["nominal_capacity"]):
            raise RuntimeError(
                f"catalogue rooms for {n['entity_id']} ({rooms.get(n['entity_id'], 0)}) "
                f"do not match its nominal_capacity ({n['nominal_capacity']})"
            )
