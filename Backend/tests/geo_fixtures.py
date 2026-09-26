"""Synthetic Overpass-format payloads for tests (no network).

``city(lat, lon, ...)`` returns the JSON an Overpass instance would return for a
grid city around a stadium: primary/secondary/tertiary roads (one oneway, one
with maxspeed/lanes), a stadium polygon with mapped entrances, metro stations,
bus stops that cluster, parking (one private), hotels (one with a room count)
and emergency facilities — plus deliberately malformed and duplicate elements.

``SnapshotLike`` emulates Overpass' server-side ``around`` filter so a smaller
radius really returns fewer roads.
"""
from __future__ import annotations

import copy
import math

from app.geospatial.footprint import Footprint, destination, haversine_m
from app.geospatial.overpass import SnapshotProvider


def _pt(lat0, lon0, north_m, east_m):
    lat, _ = destination(lat0, lon0, 0.0 if north_m >= 0 else 180.0, abs(north_m))
    _, lon = destination(lat, lon0, 90.0 if east_m >= 0 else 270.0, abs(east_m))
    return round(lat, 7), round(lon, 7)


def city(lat0: float, lon0: float, *, spacing: float = 400.0, half: int = 8, base_id: int = 1_000_000,
         with_entrances: bool = True, with_transit: bool = True, extra_hotels: int = 0) -> dict:
    els = []
    nid = lambda i, j: base_id + (i + half) * 100 + (j + half)          # noqa: E731
    coords = {}
    for i in range(-half, half + 1):
        for j in range(-half, half + 1):
            coords[nid(i, j)] = _pt(lat0, lon0, i * spacing, j * spacing)
    way_id = base_id * 10
    # east-west streets (row i) and north-south avenues (col j)
    for axis in ("row", "col"):
        for k in range(-half, half + 1):
            way_id += 1
            ids = [nid(k, j) if axis == "row" else nid(j, k) for j in range(-half, half + 1)]
            cls = "primary" if k == 0 else "secondary" if k % 3 == 0 else "tertiary"
            tags = {"highway": cls, "name": f"{'Row' if axis == 'row' else 'Col'} {k} Road"}
            if axis == "row" and k == 1:
                tags["oneway"] = "yes"
            if axis == "col" and k == 0:
                tags.update({"maxspeed": "60", "lanes": "4"})
            els.append({"type": "way", "id": way_id, "tags": tags, "nodes": ids,
                        "geometry": [{"lat": coords[n][0], "lon": coords[n][1]} for n in ids]})
    # malformed road + duplicate road
    els.append({"type": "way", "id": way_id + 1, "tags": {"highway": "primary"}, "nodes": [1, 2]})
    els.append(copy.deepcopy(els[0]))
    # stadium polygon (~300 m square) centred slightly off the junction
    sq = [_pt(lat0, lon0, n, e) for n, e in ((150, -150), (150, 150), (-150, 150), (-150, -150), (150, -150))]
    els.append({"type": "way", "id": base_id * 20, "tags": {"leisure": "stadium", "name": "Test Stadium"},
                "nodes": [base_id * 20 + k for k in range(5)], "geometry": [{"lat": a, "lon": b} for a, b in sq]})
    if with_entrances:
        for k, (n, e, kind) in enumerate(((150, 0, "main"), (0, 150, "yes"), (-150, 0, "yes"), (0, -150, "exit"))):
            la, lo = _pt(lat0, lon0, n, e)
            els.append({"type": "node", "id": base_id * 30 + k, "lat": la, "lon": lo, "tags": {"entrance": kind}})
    pid = base_id * 40

    def poi(tags, n, e, kind="node", **extra):
        nonlocal pid
        pid += 1
        la, lo = _pt(lat0, lon0, n, e)
        el = {"type": kind, "id": pid, "tags": tags, **extra}
        if kind == "node":
            el.update({"lat": la, "lon": lo})
        else:
            el["center"] = {"lat": la, "lon": lo}
        els.append(el)
        return el
    if with_transit:
        poi({"railway": "station", "station": "subway", "name": "Stadium Metro"}, 600, 420)
        poi({"public_transport": "station", "station": "subway", "name": "Stadium Metro"}, 640, 430, kind="way")  # dup
        poi({"railway": "station", "name": "Central Rail"}, -1600, -1200)
        poi({"railway": "station", "station": "subway", "name": "Far Metro"}, 2400, 2400)
        for k, (n, e) in enumerate(((-420, 380), (-460, 420), (-380, 460), (800, -800), (1200, 1210))):
            poi({"highway": "bus_stop", "name": f"Stop {k}"}, n, e)
    poi({"amenity": "parking", "capacity": "400", "name": "Stadium Parking"}, -420, -400)
    ring = [_pt(lat0, lon0, n, e) for n, e in ((820, -40), (820, 40), (780, 40), (780, -40), (820, -40))]
    poi({"amenity": "parking"}, 800, 0, kind="way", geometry=[{"lat": a, "lon": b} for a, b in ring])
    poi({"amenity": "parking", "access": "private"}, 1000, 1000)
    poi({"amenity": "parking"}, 1800, -1800)
    poi({"tourism": "hotel", "name": "Grand Hotel", "rooms": "220", "stars": "5"}, 820, 390)
    poi({"tourism": "hostel", "name": "Backpack Inn"}, -900, 380)
    poi({"tourism": "guest_house", "name": "Quiet House", "beds": "18"}, 1300, -1250)
    for k in range(extra_hotels):
        poi({"tourism": "hotel", "name": f"Hotel {k}"}, -2000 + 90 * k, 2000)
    poi({"amenity": "hospital", "name": "City Hospital"}, -820, -800, kind="way")
    poi({"amenity": "police", "name": "Police Post"}, 410, -830)
    poi({"amenity": "fire_station", "name": "Fire Station"}, 2500, -2500)
    # junk
    els.append({"type": "node", "id": base_id * 50, "tags": {"amenity": "hospital"}})          # no coordinates
    els.append({"type": "node", "tags": {"amenity": "parking"}, "lat": lat0, "lon": lon0})     # no id
    els.append({"type": "node", "id": base_id * 50 + 1, "lat": lat0, "lon": lon0, "tags": {"shop": "bakery"}})
    return {"version": 0.6, "osm3s": {"timestamp_osm_base": "2026-09-26T00:00:00Z"}, "elements": els}


class SnapshotLike(SnapshotProvider):
    """Snapshot that also applies the server-side ``around`` filter to roads."""

    def fetch(self, footprint: Footprint, venue: dict):
        payload = copy.deepcopy(self.payload)
        keep = []
        for el in payload["elements"]:
            if el.get("type") == "way" and "highway" in (el.get("tags") or {}) and el.get("geometry"):
                if not any(footprint.contains(g["lat"], g["lon"]) for g in el["geometry"]):
                    continue
            keep.append(el)
        payload["elements"] = keep
        return SnapshotProvider(payload, self.road_classes).fetch(footprint, venue)


def venue_at(lat, lon, name="Test Stadium", source="coordinates"):
    from app.geospatial.venues import CoordinateVenueProvider
    v = CoordinateVenueProvider.at(lat, lon, name)
    return v


def dist(a, b):
    return haversine_m(a["lat"], a["lon"], b["lat"], b["lon"])


__all__ = ["city", "SnapshotLike", "venue_at", "dist", "math"]
