"""Hotel capacity from OSM tags: precedence, provenance, parsing, determinism (no network)."""
from __future__ import annotations

import copy

import pytest

from app.config import get_config
from app.geospatial.blueprint import BlueprintBuilder, hotel_capacity, parse_capacity
from app.geospatial.footprint import Footprint
from app.topology import SEGMENTS
from tests.geo_fixtures import SnapshotLike, _pt, city, venue_at

LAT, LON = 23.0917717, 72.5973345
CFG = {**get_config().raw["geospatial"]["builder"], "guests_per_room": 2.0}
DEFAULTS = CFG["hotel_default_rooms"]


@pytest.mark.parametrize("raw,expected", [
    ("235", 235), (" 235 ", 235), ("235.0", 235), ("12.6", 13), ("unknown", None), ("-3", None), ("0", None),
    ("200-250", None), ("~200", None), ("1e3", None), ("", None), (None, None), ("999999", None),
])
def test_capacity_tag_parser(raw, expected):
    assert parse_capacity(raw) == expected


def test_rooms_tag_is_mapped_high_confidence():
    c = hotel_capacity({"tourism": "hotel", "rooms": "200"}, "hotel", 2.0, DEFAULTS)
    assert (c["room_capacity"], c["rooms_source"], c["rooms_confidence"]) == (200, "osm_rooms", "high")
    assert c["bed_capacity"] is None and c["effective_guest_capacity"] == 400.0


def test_beds_only_keeps_beds_and_derives_rooms_explicitly():
    c = hotel_capacity({"tourism": "hotel", "beds": "300"}, "hotel", 2.0, DEFAULTS)
    assert c["bed_capacity"] == 300 and c["bed_source"] == "osm_beds"
    assert c["room_capacity"] == 150 and c["rooms_source"] == "derived_from_osm_beds" and c["rooms_confidence"] == "medium"
    assert c["effective_guest_capacity"] == 300.0                     # guests limited by beds, not rooms x 2


def test_capacity_rooms_takes_precedence_and_beds_constrain_guests():
    c = hotel_capacity({"capacity:rooms": "250", "rooms": "240", "beds": "400"}, "hotel", 2.0, DEFAULTS)
    assert (c["room_capacity"], c["rooms_source"]) == (250, "osm_capacity_rooms")
    assert c["effective_guest_capacity"] == 400.0                      # min(250 x 2, 400)
    assert c["capacity_tags"] == {"capacity:rooms": "250", "rooms": "240", "beds": "400"}


def test_malformed_and_invalid_values_fall_through():
    c = hotel_capacity({"rooms": "unknown", "beds": "-4", "capacity:persons": "60"}, "hostel", 2.0, DEFAULTS)
    assert c["rooms_source"] == "derived_from_osm_capacity_persons" and c["room_capacity"] == 30
    e = hotel_capacity({"rooms": "0", "beds": "lots"}, "guest_house", 2.0, DEFAULTS)
    assert (e["rooms_source"], e["rooms_confidence"], e["room_capacity"]) == ("derived_estimate", "low", DEFAULTS["guest_house"])


def _payload_with_hotels():
    p = city(LAT, LON)
    for k, (tags, n, e) in enumerate((
        ({"tourism": "hotel", "name": "Mapped Rooms Hotel", "rooms": "200"}, 500, -300),
        ({"tourism": "hotel", "name": "Beds Only Hotel", "beds": "300"}, -500, 300),
        ({"tourism": "hotel", "name": "Capacity Rooms Hotel", "capacity:rooms": "250", "rooms": "x"}, 300, 700),
        ({"tourism": "hotel", "name": "Broken Hotel", "rooms": "unknown", "beds": "0"}, -300, -700),
    )):
        la, lo = _pt(LAT, LON, n, e)
        p["elements"].append({"type": "node", "id": 7_000_000 + k, "lat": la, "lon": lo, "tags": tags})
    return p


def _build(payload):
    fp = Footprint(LAT, LON, 2000)
    v = venue_at(LAT, LON)
    return BlueprintBuilder(CFG, SEGMENTS).build(v, fp, SnapshotLike(payload).fetch(fp, v), data_source="osm_snapshot")


def test_blueprint_hotels_keep_their_own_provenance():
    bp = _build(_payload_with_hotels())
    props = {p["name"]: p for p in bp["properties"]}
    assert props["Mapped Rooms Hotel"]["rooms_total"] == 200 and props["Mapped Rooms Hotel"]["rooms_source"] == "osm_rooms"
    assert props["Beds Only Hotel"]["bed_capacity"] == 300 and props["Beds Only Hotel"]["rooms_source"] == "derived_from_osm_beds"
    assert props["Capacity Rooms Hotel"]["rooms_total"] == 250 and props["Capacity Rooms Hotel"]["rooms_source"] == "osm_capacity_rooms"
    assert props["Broken Hotel"]["rooms_source"] == "derived_estimate" and props["Broken Hotel"]["rooms_confidence"] == "low"
    nodes = {n["entity_id"]: n for n in bp["nodes"]}
    for p in bp["properties"]:                                        # entity capacity == rooms, same provenance
        n = nodes[p["cluster_entity_id"]]
        assert n["nominal_capacity"] == p["rooms_total"] and n["capacity_source"] == p["rooms_source"]
        assert "capacity_tags" in n["meta"]


def test_hotel_capacity_is_part_of_the_canonical_blueprint():
    a = _build(_payload_with_hotels())
    shuffled = _payload_with_hotels()
    shuffled["elements"] = list(reversed(shuffled["elements"]))
    assert _build(shuffled)["graph_hash"] == a["graph_hash"]
    changed = _payload_with_hotels()
    for el in changed["elements"]:
        if (el.get("tags") or {}).get("name") == "Mapped Rooms Hotel":
            el["tags"]["rooms"] = "201"
    assert _build(changed)["graph_hash"] != a["graph_hash"]            # capacity participates in the hash
