"""Generic event-graph validation.

These are graph invariants, not demo assumptions: no node count minimum, no
required entity IDs, no required edges. A report is returned instead of an
exception deep inside the simulator, so an incomplete graph fails with a
readable reason (e.g. "the venue has no connected access point").
"""
from __future__ import annotations

import math
from collections import deque
from typing import Any

ENTITY_TYPES = {"venue", "gate", "transport_node", "transport_route", "road", "zone", "hotel", "parking",
                "emergency_facility"}
EDGE_TYPES = {"feeds", "adjacent_to", "serves", "last_mile_to", "evacuates_to", "substitutes_for", "connects_to"}
ACCESS_TYPES = {"transport_node", "parking"}
CONFIDENCE = {"high", "medium", "low"}


def validate_topology(topology: dict[str, Any], *, require_provenance: bool = False,
                      properties: list[dict] | None = None) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    checks: dict[str, bool] = {}
    nodes = topology.get("nodes") or []
    edges = topology.get("edges") or []

    def check(name: str, ok: bool, message: str, severity: str = "error") -> None:
        checks[name] = bool(ok)
        if not ok:
            (errors if severity == "error" else warnings).append(message)

    check("non_empty", bool(nodes), "The graph has no entities.")
    ids = [n.get("entity_id") for n in nodes]
    check("unique_node_ids", len(ids) == len(set(ids)), "Entity ids are not unique.")
    eids = [e.get("edge_id") for e in edges]
    check("unique_edge_ids", len(eids) == len(set(eids)), "Edge ids are not unique.")
    by_id = {n.get("entity_id"): n for n in nodes}
    bad_refs = [e.get("edge_id") for e in edges if e.get("src_entity_id") not in by_id or e.get("dst_entity_id") not in by_id]
    check("edge_references", not bad_refs, f"{len(bad_refs)} edge(s) reference unknown entities (e.g. {bad_refs[:3]}).")
    loops = [e.get("edge_id") for e in edges if e.get("src_entity_id") == e.get("dst_entity_id")]
    check("no_self_loops", not loops, f"{len(loops)} self-loop edge(s).")
    bad_coords = [n.get("entity_id") for n in nodes if not _finite(n.get("lat")) or not _finite(n.get("lon"))
                  or not -90 <= float(n.get("lat", 0)) <= 90 or not -180 <= float(n.get("lon", 0)) <= 180]
    check("valid_coordinates", not bad_coords, f"{len(bad_coords)} entity(ies) with invalid coordinates.")
    bad_types = sorted({n.get("entity_type") for n in nodes if n.get("entity_type") not in ENTITY_TYPES}, key=str)
    check("semantic_node_types", not bad_types, f"Unsupported entity types: {bad_types}.")
    bad_etypes = sorted({e.get("edge_type") for e in edges if e.get("edge_type") not in EDGE_TYPES}, key=str)
    check("semantic_edge_types", not bad_etypes, f"Unsupported edge types: {bad_etypes}.")
    bad_cap = [n.get("entity_id") for n in nodes if not _finite(n.get("nominal_capacity")) or float(n["nominal_capacity"]) <= 0]
    check("capacities", not bad_cap, f"{len(bad_cap)} entity(ies) without a positive capacity.")
    bad_dist = [e.get("edge_id") for e in edges if e.get("distance_m") is not None
                and (not _finite(e["distance_m"]) or float(e["distance_m"]) < 0)]
    check("edge_distances", not bad_dist, f"{len(bad_dist)} edge(s) with an invalid distance.")
    bad_tt = [e.get("edge_id") for e in edges if not _finite(e.get("travel_time_sec")) or float(e["travel_time_sec"]) < 0]
    check("travel_times", not bad_tt, f"{len(bad_tt)} edge(s) with an invalid travel time.")
    check("deterministic_order", ids == sorted(ids, key=str) and eids == sorted(eids, key=str),
          "Entities/edges are not in canonical (sorted) order.", severity="warning")

    venues = [n["entity_id"] for n in nodes if n.get("entity_type") == "venue"]
    check("venue_exists", bool(venues), "The graph has no venue.")
    gate_venue = {}
    for e in edges:
        s, d = by_id.get(e.get("src_entity_id")), by_id.get(e.get("dst_entity_id"))
        if s and d and s.get("entity_type") == "gate" and d.get("entity_type") == "venue" and e.get("edge_type") == "feeds":
            gate_venue.setdefault(d["entity_id"], []).append(s["entity_id"])
    reachable_venues = []
    for v in venues:
        arrivals = [e for e in edges if e.get("edge_type") in ("feeds", "serves", "last_mile_to")
                    and by_id.get(e.get("src_entity_id"), {}).get("entity_type") in ACCESS_TYPES
                    and (e.get("dst_entity_id") == v or e.get("dst_entity_id") in gate_venue.get(v, []))]
        if arrivals:
            reachable_venues.append(v)
    check("venue_connected", not venues or bool(reachable_venues),
          "No venue is connected to the network: there is no station, bus stop or car park that leads to an access point.")
    check("arrival_path", not venues or bool(reachable_venues), "No arrival path from any access node to a venue.")

    # Egress: the venue's access points connect back into the road/transport network.
    egress_ok = True
    for v in reachable_venues:
        gates = gate_venue.get(v, [])
        if gates and not any(e.get("src_entity_id") in gates and by_id.get(e.get("dst_entity_id"), {}).get("entity_type")
                             in ("road", "zone") for e in edges):
            egress_ok = False
    check("egress_path", egress_ok, "A venue's access points do not connect back to the road network (no egress).",
          severity="warning")

    # Isolated critical components: every gate / access node must be connected to the rest.
    adj: dict[str, list[str]] = {}
    for e in edges:
        a, b = e.get("src_entity_id"), e.get("dst_entity_id")
        if a in by_id and b in by_id and e.get("edge_type") != "substitutes_for":
            adj.setdefault(a, []).append(b)
            adj.setdefault(b, []).append(a)
    if venues:
        seen = {venues[0]}
        q = deque([venues[0]])
        while q:
            x = q.popleft()
            for y in adj.get(x, []):
                if y not in seen:
                    seen.add(y)
                    q.append(y)
        isolated_critical = [n["entity_id"] for n in nodes if n.get("entity_type") in ("gate",) and n["entity_id"] not in seen]
        check("no_isolated_critical", not isolated_critical,
              f"{len(isolated_critical)} access point(s) are not connected to the venue's network.")
        isolated_other = [n["entity_id"] for n in nodes if n["entity_id"] not in seen]
        if isolated_other:
            warnings.append(f"{len(isolated_other)} entity(ies) are not connected to the venue's network.")

    if require_provenance:
        missing = [n.get("entity_id") for n in nodes if not isinstance(n.get("provenance"), dict)
                   or n["provenance"].get("confidence") not in CONFIDENCE or "source" not in n["provenance"]]
        missing += [e.get("edge_id") for e in edges if not isinstance(e.get("provenance"), dict)]
        check("provenance", not missing, f"{len(missing)} generated entity/edge(s) lack provenance.")
        no_cap_src = [n.get("entity_id") for n in nodes if not n.get("capacity_source")
                      or n.get("capacity_confidence") not in CONFIDENCE]
        check("capacity_provenance", not no_cap_src, f"{len(no_cap_src)} entity(ies) lack capacity source/confidence.")

    if properties is not None:
        hotel_ids = {n["entity_id"] for n in nodes if n.get("entity_type") == "hotel"}
        orphan = [p.get("property_id") for p in properties if p.get("cluster_entity_id") not in hotel_ids]
        check("properties_reference_hotels", not orphan, f"{len(orphan)} hotel propert(ies) reference unknown hotels.")

    return {"valid": not errors, "errors": errors, "warnings": warnings, "checks": checks,
            "counts": {"nodes": len(nodes), "edges": len(edges)}}


def _finite(x: Any) -> bool:
    try:
        return x is not None and not isinstance(x, bool) and math.isfinite(float(x))
    except (TypeError, ValueError):
        return False
