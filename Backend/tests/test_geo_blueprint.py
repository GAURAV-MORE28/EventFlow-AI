"""OSM parsing and the BlueprintBuilder, on synthetic Overpass payloads (no network)."""
from __future__ import annotations

import re

import pytest

from app.config import get_config
from app.geospatial.blueprint import BlueprintBuilder, BlueprintError, parse_speed_kmh
from app.geospatial.footprint import Footprint, haversine_m
from app.geospatial.overpass import DEFAULT_ROAD_CLASSES, build_query, parse_overpass
from app.geospatial.validation import validate_topology
from app.topology import SEGMENTS
from tests.geo_fixtures import SnapshotLike, city, venue_at

LAT, LON = 23.0917717, 72.5973345
CFG = get_config().raw["geospatial"]["builder"]


def build(lat=LAT, lon=LON, radius=2000, payload=None, **kw):
    payload = payload or city(lat, lon)
    fp = Footprint(lat, lon, radius)
    venue = venue_at(lat, lon)
    raw = SnapshotLike(payload).fetch(fp, venue)
    return BlueprintBuilder(CFG, SEGMENTS).build(venue, fp, raw, data_source="osm_snapshot", **kw), raw


# --- D. OSM provider --------------------------------------------------------------------------------
def test_query_is_focused_and_bounded():
    q = build_query(Footprint(LAT, LON, 1500), {"osm_type": "way", "osm_id": 776718456, "lat": LAT, "lon": LON},
                    DEFAULT_ROAD_CLASSES)
    assert "[timeout:60]" in q and "[maxsize:" in q and "around:1500,23.0917717,72.5973345" in q
    assert "way(776718456)" in q
    assert "nwr(around" in q and "[amenity=parking]" in q and "shop" not in q and "[amenity]" not in q


def test_parser_classifies_dedupes_and_skips_malformed():
    fp = Footprint(LAT, LON, 5000)
    raw = parse_overpass(city(LAT, LON), fp, venue_at(LAT, LON), DEFAULT_ROAD_CLASSES)
    assert raw.skipped["road_without_geometry"] == 1 and raw.skipped["duplicate_element"] >= 1
    assert raw.skipped["malformed_element"] == 1 and raw.skipped["missing_coordinates"] == 1
    assert raw.skipped["parking_not_public"] == 1 and raw.skipped["unsupported_category"] == 1
    subtypes = sorted(t["subtype"] for t in raw.transit)
    assert subtypes.count("metro_station") == 3 and subtypes.count("bus_stop") == 5 and "rail_station" in subtypes
    assert {h["subtype"] for h in raw.hotels} == {"hotel", "hostel", "guest_house"}
    assert {e["subtype"] for e in raw.emergency} == {"hospital", "police", "fire_station"}
    assert raw.venue_geometry and len(raw.entrances) == 4
    assert [r["osm_id"] for r in raw.roads] == sorted(r["osm_id"] for r in raw.roads)
    assert any(p.get("area_m2") for p in raw.parking)                 # polygon area kept for capacity


def test_parser_rejects_non_overpass_payload():
    from app.geospatial.http import ProviderError
    with pytest.raises(ProviderError):
        parse_overpass({"remark": "runtime error"}, Footprint(LAT, LON, 1000), venue_at(LAT, LON), DEFAULT_ROAD_CLASSES)


def test_speed_parser():
    assert parse_speed_kmh("50") == 50 and round(parse_speed_kmh("30 mph"), 1) == 48.3
    assert parse_speed_kmh("walk") is None and parse_speed_kmh("999") is None


# --- E. blueprint builder ------------------------------------------------------------------------------
def test_blueprint_is_a_valid_engine_topology_with_provenance():
    bp, _ = build()
    report = validate_topology(bp, require_provenance=True, properties=bp["properties"])
    assert report["valid"], report["errors"]
    types = {n["entity_type"] for n in bp["nodes"]}
    assert types == {"road", "venue", "gate", "transport_node", "parking", "hotel", "emergency_facility", "zone"}
    for n in bp["nodes"]:
        assert re.fullmatch(r"n_[0-9a-f]{10}", n["entity_id"]), n["entity_id"]      # opaque ids
        assert n["capacity_source"] and n["capacity_confidence"] in ("high", "medium", "low")
        assert n["provenance"]["source"] in ("osm", "organizer", "derived")
    for e in bp["edges"]:
        assert re.fullmatch(r"e_[0-9a-f]{10}", e["edge_id"]) and e["travel_time_source"]
        assert e["travel_time_sec"] >= 1 and e["src_entity_id"] != e["dst_entity_id"]


def test_roads_become_a_junction_graph_with_geometry_oneway_and_travel_time():
    bp, raw = build(radius=1000)
    roads = {n["entity_id"]: n for n in bp["nodes"] if n["entity_type"] == "road"}
    segs = [e for e in bp["edges"] if e["edge_type"] == "adjacent_to" and e["src_entity_id"] in roads
            and e["dst_entity_id"] in roads]
    assert len(roads) > 10 and len(segs) > len(roads)            # a grid, not one collapsed node
    for s in segs:
        assert s["distance_m"] > 0 and len(s["geometry"]) >= 2
        a, b = roads[s["src_entity_id"]], roads[s["dst_entity_id"]]
        assert abs(haversine_m(a["lat"], a["lon"], s["geometry"][0][1], s["geometry"][0][0])) < 25
    oneway = [s for s in segs if s["directionality"] == "oneway"]
    assert oneway and all("Row 1 Road" in str(roads[s["src_entity_id"]]["meta"]["road_names"]) for s in oneway)
    # maxspeed 60 on Col 0 → faster than the 30 km/h tertiary default over the same 400 m block
    fast = [s for s in segs if s["travel_time_source"] == "osm_maxspeed"]
    slow = [s for s in segs if s["travel_time_source"] == "derived_highway_class"]
    assert fast and slow
    f = min(fast, key=lambda s: abs(s["distance_m"] - 400))
    assert f["travel_time_sec"] == pytest.approx(f["distance_m"] / (60 / 3.6), abs=1.5)
    assert f["capacity_per_min"] == 2 * CFG["lane_capacity_veh_per_min"]      # lanes=4, two-way → 2 per direction


def test_access_points_come_from_mapped_entrances_and_connect_to_roads():
    bp, _ = build()
    gates = [n for n in bp["nodes"] if n["entity_type"] == "gate"]
    mapped = [g for g in gates if g["meta"]["evidence"].startswith("mapped entrance")]
    assert len(mapped) == 3                               # 4 entrances, the exit-only one is not an entry
    # fewer than min_access_points mapped: the uncovered sector with a road approach (W) is added
    extra = [g for g in gates if g not in mapped]
    assert [g["display_name"] for g in extra] == ["Access W"] and extra[0]["provenance"]["source"] == "derived"
    assert all(g["meta"]["gate_inference"] == "mapped_entrances+road_approaches" and g["provenance"]["inferred"]
               for g in gates)
    venue = next(n for n in bp["nodes"] if n["entity_type"] == "venue")
    for g in gates:
        out = [e for e in bp["edges"] if e["src_entity_id"] == g["entity_id"]]
        assert any(e["dst_entity_id"] == venue["entity_id"] and e["edge_type"] == "feeds" for e in out)
        assert any(e["edge_type"] == "adjacent_to" for e in out)            # spill onto its road
    # without mapped entrances: road approaches by bearing sector, bounded and evidence-based
    bp2, _ = build(payload=city(LAT, LON, with_entrances=False))
    g2 = [n for n in bp2["nodes"] if n["entity_type"] == "gate"]
    assert 1 <= len(g2) <= CFG["max_gates"] and all(g["meta"]["gate_inference"] == "road_approaches" for g in g2)


def test_access_nodes_route_to_gates_along_road_paths():
    bp, _ = build()
    by = {n["entity_id"]: n for n in bp["nodes"]}
    routes = [e for e in bp["edges"] if e["edge_type"] in ("feeds", "serves")
              and by[e["src_entity_id"]]["entity_type"] in ("transport_node", "parking")
              and by[e["dst_entity_id"]]["entity_type"] == "gate"]
    assert routes
    for e in routes:
        assert e["via_entity_ids"] and all(by[r]["entity_type"] == "road" for r in e["via_entity_ids"])
        assert e["travel_time_source"] == "derived_shortest_path_walk"
    per_src = {}
    for e in routes:
        per_src[e["src_entity_id"]] = per_src.get(e["src_entity_id"], 0.0) + e["transfer_coefficient"]
    assert all(abs(v - 1.0) < 0.01 for v in per_src.values())           # split over gates sums to 1


def test_dedup_clustering_capacities_and_hotels():
    bp, _ = build()
    tn = [n for n in bp["nodes"] if n["entity_type"] == "transport_node"]
    metro = [n for n in tn if n["display_name"] == "Stadium Metro"]
    assert len(metro) == 1 and metro[0]["meta"]["osm_members"] == 2       # station node + station way merged
    clusters = [n for n in tn if n["subtype"] == "bus_stop_cluster"]
    assert any(n["meta"]["osm_members"] == 3 for n in clusters)          # three stops within 250 m
    parking = {n["display_name"]: n for n in bp["nodes"] if n["entity_type"] == "parking"}
    assert parking["Stadium Parking"]["nominal_capacity"] == 400 and parking["Stadium Parking"]["capacity_source"] == "osm_attribute"
    assert any(n["capacity_source"] == "derived_area" for n in parking.values())
    props = {p["name"]: p for p in bp["properties"]}
    assert props["Grand Hotel"]["rooms_total"] == 220 and props["Grand Hotel"]["rooms_source"] == "osm_rooms"
    assert props["Grand Hotel"]["tier"] == "luxury" and props["Grand Hotel"]["price_per_night_paise"] is None
    # beds=18 is kept as beds; rooms are derived from it (ceil(18 / 2.2) = 9) and say so
    assert props["Quiet House"]["rooms_total"] == 9 and props["Quiet House"]["rooms_source"] == "derived_from_osm_beds"
    assert props["Quiet House"]["bed_capacity"] == 18 and props["Quiet House"]["rooms_confidence"] == "medium"
    assert props["Backpack Inn"]["rooms_source"] == "derived_estimate" and props["Backpack Inn"]["tier"] is None
    hotels = {n["entity_id"]: n for n in bp["nodes"] if n["entity_type"] == "hotel"}
    assert all(hotels[p["cluster_entity_id"]]["nominal_capacity"] == p["rooms_total"] for p in bp["properties"])


def test_venue_capacity_hierarchy():
    organizer, _ = build(venue_capacity=55000)
    venue = next(n for n in organizer["nodes"] if n["entity_type"] == "venue")
    assert (venue["nominal_capacity"], venue["capacity_source"], venue["capacity_confidence"]) == (55000, "organizer", "high")
    derived, _ = build()
    v2 = next(n for n in derived["nodes"] if n["entity_type"] == "venue")
    assert v2["capacity_source"] == "derived_venue_area" and v2["capacity_confidence"] == "low"


def test_same_input_same_canonical_blueprint():
    a, _ = build()
    b, _ = build()
    assert a["graph_hash"] == b["graph_hash"] and a["blueprint_id"] == b["blueprint_id"]
    assert [n["entity_id"] for n in a["nodes"]] == [n["entity_id"] for n in b["nodes"]]
    assert [e["edge_id"] for e in a["edges"]] == [e["edge_id"] for e in b["edges"]]


def test_response_order_does_not_change_the_blueprint():
    payload = city(LAT, LON)
    shuffled = dict(payload, elements=list(reversed(payload["elements"])))
    assert build(payload=payload)[0]["graph_hash"] == build(payload=shuffled)[0]["graph_hash"]


def test_larger_radius_is_a_geographic_superset():
    small, raw_s = build(radius=1000)
    large, raw_l = build(radius=3000)
    assert {r["osm_id"] for r in raw_s.roads} < {r["osm_id"] for r in raw_l.roads}
    for kind in ("transport_node", "parking", "hotel", "emergency_facility"):
        s = {n["entity_id"] for n in small["nodes"] if n["entity_type"] == kind}
        l_ = {n["entity_id"] for n in large["nodes"] if n["entity_type"] == kind}
        assert s <= l_, kind
    assert large["summary"]["road_nodes"] > small["summary"]["road_nodes"]
    assert large["graph_hash"] != small["graph_hash"]


def test_different_venues_give_different_topologies():
    a, _ = build()
    b, _ = build(51.5559, -0.2796, payload=city(51.5559, -0.2796, spacing=330, half=9, extra_hotels=4, base_id=2_000_000))
    assert a["graph_hash"] != b["graph_hash"]
    assert not {n["entity_id"] for n in a["nodes"]} & {n["entity_id"] for n in b["nodes"]}
    va = next(n for n in a["nodes"] if n["entity_type"] == "venue")
    vb = next(n for n in b["nodes"] if n["entity_type"] == "venue")
    assert haversine_m(va["lat"], va["lon"], vb["lat"], vb["lon"]) > 6_000_000


def test_structured_errors_for_unusable_footprints():
    empty = {"elements": [], "osm3s": {}}
    with pytest.raises(BlueprintError) as exc:
        build(payload=empty)
    assert exc.value.code == "NO_ROAD_NETWORK"
    no_transit = city(LAT, LON, with_transit=False)
    no_transit["elements"] = [e for e in no_transit["elements"] if (e.get("tags") or {}).get("amenity") != "parking"]
    with pytest.raises(BlueprintError) as exc:
        build(payload=no_transit)
    assert exc.value.code == "NO_ARRIVAL_ACCESS" and "within" in exc.value.message


def test_access_points_in_the_same_sector_get_distinct_labels():
    from tests.geo_fixtures import _pt
    payload = city(LAT, LON, with_entrances=False)
    for k, east in enumerate((-40, 60)):               # two unnamed entrances in the N sector, 100 m apart
        la, lo = _pt(LAT, LON, 150, east)
        payload["elements"].append({"type": "node", "id": 9_000_000 + k, "lat": la, "lon": lo, "tags": {"entrance": "yes"}})
    bp, _ = build(payload=payload)
    labels = [n["display_name"] for n in bp["nodes"] if n["entity_type"] == "gate"]
    assert len(labels) == len(set(labels)), labels
    assert "Access N 1" in labels and "Access N 2" in labels


def test_precinct_links_have_distance_based_walk_times():
    bp, _ = build()
    by = {n["entity_id"]: n for n in bp["nodes"]}
    links = [e for e in bp["edges"] if by[e["dst_entity_id"]]["entity_type"] == "zone"]
    assert links and all(e["distance_m"] is not None and e["travel_time_source"] == "derived_geodesic_walk" for e in links)
    # no shortcut: gate -> precinct -> other gate is never faster than the straight walk between them
    gates = [e for e in links if by[e["src_entity_id"]]["entity_type"] == "gate"]
    for a in gates:
        for b in gates:
            if a is not b:
                from app.geospatial.footprint import haversine_m
                ga, gb = by[a["src_entity_id"]], by[b["src_entity_id"]]
                assert a["travel_time_sec"] + b["travel_time_sec"] >= haversine_m(ga["lat"], ga["lon"], gb["lat"], gb["lon"]) / 1.3 - 2
