"""GeoDataProvider: focused OSM acquisition inside a monitoring footprint.

One Overpass request per build, containing only the categories EventFlow models:

    roads      highway ways of the configured classes, with node ids + geometry
    transit    rail / metro stations, bus stations, bus stops
    parking    amenity=parking (not private), with geometry for area
    hotels     tourism=hotel|hostel|guest_house|motel|apartment
    emergency  hospital, police, fire station, ambulance station
    venue      the venue's own polygon (by OSM id when known) and mapped entrances

Nothing else is downloaded — no blanket POI harvesting. Queries use
``around:<radius>,<lat>,<lon>`` so the footprint itself bounds the request, with
server-side ``timeout`` and ``maxsize`` limits, a descriptive User-Agent, ordered
endpoint failover and a cache (``HttpClient``).

Output is a ``RawGeoData`` of plain dicts, sorted by (osm_type, osm_id) so the
same OSM snapshot always yields the same raw data, independent of response order.
OSM data © OpenStreetMap contributors, ODbL 1.0.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .footprint import Footprint
from .http import HttpClient, ProviderError

log = logging.getLogger("eventflow.geospatial.overpass")

ATTRIBUTION = "© OpenStreetMap contributors (ODbL 1.0)"
DEFAULT_ROAD_CLASSES = [
    "motorway", "trunk", "primary", "secondary", "tertiary", "unclassified",
    "motorway_link", "trunk_link", "primary_link", "secondary_link", "tertiary_link",
]
HOTEL_TYPES = ("hotel", "hostel", "guest_house", "motel", "apartment")
VENUE_TAGS = ("stadium", "sports_centre", "arena")


@dataclass
class RawGeoData:
    roads: list[dict[str, Any]] = field(default_factory=list)
    transit: list[dict[str, Any]] = field(default_factory=list)
    parking: list[dict[str, Any]] = field(default_factory=list)
    hotels: list[dict[str, Any]] = field(default_factory=list)
    emergency: list[dict[str, Any]] = field(default_factory=list)
    venue_geometry: list[list[float]] | None = None      # [[lat, lon], ...] outer ring
    venue_osm: dict[str, Any] | None = None              # {osm_type, osm_id, tags}
    entrances: list[dict[str, Any]] = field(default_factory=list)
    skipped: dict[str, int] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)

    def counts(self) -> dict[str, int]:
        return {"roads": len(self.roads), "transit": len(self.transit), "parking": len(self.parking),
                "hotels": len(self.hotels), "emergency": len(self.emergency), "entrances": len(self.entrances)}


class GeoDataProvider(Protocol):
    name: str
    live: bool

    def fetch(self, footprint: Footprint, venue: dict[str, Any]) -> RawGeoData: ...


# --- query ------------------------------------------------------------------------------------
def build_query(footprint: Footprint, venue: dict[str, Any], road_classes: list[str], timeout_sec: int = 60,
                maxsize_bytes: int = 64 * 1024 * 1024, entrance_radius_m: float = 700.0) -> str:
    r = int(round(footprint.radius_m))
    at = f"{footprint.center_lat:.7f},{footprint.center_lon:.7f}"
    around = f"(around:{r},{at})"
    classes = "|".join(sorted(set(road_classes)))
    er = int(min(entrance_radius_m, footprint.radius_m))
    venue_stmt = ""
    if venue.get("osm_type") in ("way", "relation") and isinstance(venue.get("osm_id"), int):
        venue_stmt = f"{venue['osm_type']}({int(venue['osm_id'])});out body geom;"
    else:
        venue_stmt = (f"(way(around:150,{at})[leisure~\"^({'|'.join(VENUE_TAGS)})$\"];"
                      f"relation(around:150,{at})[leisure~\"^({'|'.join(VENUE_TAGS)})$\"];);out body geom;")
    return (
        f"[out:json][timeout:{int(timeout_sec)}][maxsize:{int(maxsize_bytes)}];"
        f"way{around}[highway~\"^({classes})$\"];out body geom;"
        "("
        f"nwr{around}[railway~\"^(station|halt)$\"];"
        f"nwr{around}[station~\"^(subway|light_rail|monorail)$\"];"
        f"nwr{around}[public_transport=station];"
        f"nwr{around}[amenity=bus_station];"
        f"node{around}[highway=bus_stop];"
        f"nwr{around}[tourism~\"^({'|'.join(HOTEL_TYPES)})$\"];"
        f"nwr{around}[amenity~\"^(hospital|police|fire_station)$\"];"
        f"nwr{around}[emergency=ambulance_station];"
        ");out tags center;"
        f"nwr{around}[amenity=parking];out tags geom;"
        f"{venue_stmt}"
        f"node(around:{er},{at})[entrance];out body;"
    )


# --- parsing ----------------------------------------------------------------------------------
def _point(el: dict) -> tuple[float, float] | None:
    try:
        if el.get("type") == "node":
            lat, lon = float(el["lat"]), float(el["lon"])
        elif isinstance(el.get("center"), dict):
            lat, lon = float(el["center"]["lat"]), float(el["center"]["lon"])
        elif el.get("geometry"):
            pts = [(float(g["lat"]), float(g["lon"])) for g in el["geometry"] if g]
            lat, lon = sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)
        elif isinstance(el.get("bounds"), dict):
            b = el["bounds"]
            lat, lon = (float(b["minlat"]) + float(b["maxlat"])) / 2, (float(b["minlon"]) + float(b["maxlon"])) / 2
        else:
            return None
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or lat != lat or lon != lon:
        return None
    return round(lat, 7), round(lon, 7)


def _ring(el: dict) -> list[list[float]] | None:
    """Outer ring [[lat, lon], ...] of a way or (largest outer member of) a relation."""
    try:
        if el.get("type") == "way" and el.get("geometry"):
            pts = [[round(float(g["lat"]), 7), round(float(g["lon"]), 7)] for g in el["geometry"] if g]
        elif el.get("type") == "relation":
            outers = [m for m in el.get("members", []) if m.get("role") in ("outer", "") and m.get("geometry")]
            if not outers:
                return None
            best = max(outers, key=lambda m: len(m["geometry"]))
            pts = [[round(float(g["lat"]), 7), round(float(g["lon"]), 7)] for g in best["geometry"] if g]
        else:
            return None
    except (KeyError, TypeError, ValueError):
        return None
    return pts if len(pts) >= 4 else None


def transit_subtype(tags: dict[str, str]) -> str | None:
    if tags.get("station") in ("subway", "light_rail", "monorail") or tags.get("subway") == "yes" \
            or tags.get("light_rail") == "yes":
        return "metro_station"
    if tags.get("railway") in ("station", "halt"):
        return "rail_station"
    if tags.get("amenity") == "bus_station" or (tags.get("public_transport") == "station" and tags.get("bus") == "yes"):
        return "bus_station"
    if tags.get("public_transport") == "station":
        return "rail_station" if tags.get("train") == "yes" else "bus_station"
    if tags.get("highway") == "bus_stop":
        return "bus_stop"
    return None


def emergency_subtype(tags: dict[str, str]) -> str | None:
    if tags.get("emergency") == "ambulance_station":
        return "ambulance_station"
    return {"hospital": "hospital", "police": "police", "fire_station": "fire_station"}.get(tags.get("amenity", ""))


def parse_overpass(payload: Any, footprint: Footprint, venue: dict[str, Any], road_classes: list[str]) -> RawGeoData:
    if not isinstance(payload, dict) or not isinstance(payload.get("elements"), list):
        raise ProviderError("overpass", "response has no elements array")
    raw = RawGeoData()
    skipped: dict[str, int] = {}
    classes = set(road_classes)
    seen: set[tuple[str, int]] = set()

    def skip(reason: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1

    venue_key = (venue.get("osm_type"), venue.get("osm_id"))
    venue_candidates = []
    for el in payload["elements"]:
        if not isinstance(el, dict) or el.get("type") not in ("node", "way", "relation") \
                or not isinstance(el.get("id"), int):
            skip("malformed_element")
            continue
        key = (el["type"], el["id"])
        tags = el.get("tags") if isinstance(el.get("tags"), dict) else {}
        tags = {str(k): str(v) for k, v in tags.items()}
        ref = {"osm_type": el["type"], "osm_id": el["id"], "tags": tags}

        if (el["type"], el["id"]) == venue_key or (el["type"] != "node" and tags.get("leisure") in VENUE_TAGS
                                                   and key not in seen and "highway" not in tags):
            ring = _ring(el)
            if ring:
                venue_candidates.append((0 if key == venue_key else 1, el["id"], ring, ref))
            if key == venue_key:
                continue
        if el["type"] == "way" and tags.get("highway") in classes:
            if key in seen:
                skip("duplicate_element")
                continue
            nodes, geom = el.get("nodes"), el.get("geometry")
            if not isinstance(nodes, list) or not isinstance(geom, list) or len(nodes) != len(geom) or len(nodes) < 2:
                skip("road_without_geometry")
                continue
            try:
                pts = [(round(float(g["lat"]), 7), round(float(g["lon"]), 7)) for g in geom]
            except (KeyError, TypeError, ValueError):
                skip("road_without_geometry")
                continue
            seen.add(key)
            raw.roads.append({**ref, "nodes": [int(n) for n in nodes], "geometry": pts})
            continue
        if "entrance" in tags and el["type"] == "node":
            p = _point(el)
            if p and key not in seen:
                seen.add(key)
                raw.entrances.append({**ref, "lat": p[0], "lon": p[1]})
            continue
        category, subtype = None, None
        if transit_subtype(tags):
            category, subtype = "transit", transit_subtype(tags)
        elif tags.get("amenity") == "parking":
            if tags.get("access") in ("private", "no", "customers") :
                skip("parking_not_public")
                continue
            category, subtype = "parking", tags.get("parking") or "surface"
        elif tags.get("tourism") in HOTEL_TYPES:
            category, subtype = "hotel", tags["tourism"]
        elif emergency_subtype(tags):
            category, subtype = "emergency", emergency_subtype(tags)
        else:
            if tags.get("leisure") in VENUE_TAGS:
                continue
            skip("unsupported_category")
            continue
        if key in seen:
            skip("duplicate_element")
            continue
        p = _point(el)
        if p is None:
            skip("missing_coordinates")
            continue
        if not footprint.contains(p[0], p[1], slack_m=1.0):
            skip("outside_footprint")
            continue
        seen.add(key)
        item = {**ref, "lat": p[0], "lon": p[1], "subtype": subtype}
        if category == "parking":
            ring = _ring(el)
            if ring:
                item["area_m2"] = round(polygon_area_m2(ring), 1)
        getattr(raw, {"transit": "transit", "parking": "parking", "hotel": "hotels",
                      "emergency": "emergency"}[category]).append(item)

    vlat, vlon = float(venue["lat"]), float(venue["lon"])
    ranked = []
    for exact, oid, ring, ref in venue_candidates:
        inside = point_in_ring(vlat, vlon, ring)
        clat, clon = sum(p[0] for p in ring) / len(ring), sum(p[1] for p in ring) / len(ring)
        dist = footprint_distance(vlat, vlon, clat, clon)
        if exact == 0 or inside or dist <= 150.0:
            ranked.append((exact, 0 if inside else 1, round(dist, 1), oid, ring, ref))
    if ranked:
        ranked.sort(key=lambda c: c[:4])
        raw.venue_geometry, raw.venue_osm = ranked[0][4], ranked[0][5]
    for lst in (raw.roads, raw.transit, raw.parking, raw.hotels, raw.emergency, raw.entrances):
        lst.sort(key=lambda x: (x["osm_type"], x["osm_id"]))
    raw.skipped = dict(sorted(skipped.items()))
    osm3s = payload.get("osm3s") if isinstance(payload.get("osm3s"), dict) else {}
    raw.meta = {"osm_base_timestamp": osm3s.get("timestamp_osm_base"), "attribution": ATTRIBUTION}
    return raw


def point_in_ring(lat: float, lon: float, ring: list[list[float]]) -> bool:
    inside = False
    n = len(ring)
    for i in range(n):
        y1, x1 = ring[i]
        y2, x2 = ring[(i + 1) % n]
        if (y1 > lat) != (y2 > lat):
            x = x1 + (lat - y1) * (x2 - x1) / ((y2 - y1) or 1e-12)
            if lon < x:
                inside = not inside
    return inside


def footprint_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    from .footprint import haversine_m
    return haversine_m(lat1, lon1, lat2, lon2)


def polygon_area_m2(ring: list[list[float]]) -> float:
    """Shoelace area on a local equirectangular projection (fine for < a few km)."""
    import math
    if len(ring) < 3:
        return 0.0
    lat0 = math.radians(sum(p[0] for p in ring) / len(ring))
    xy = [(math.radians(p[1]) * math.cos(lat0) * 6_371_000.0, math.radians(p[0]) * 6_371_000.0) for p in ring]
    s = 0.0
    for (x1, y1), (x2, y2) in zip(xy, xy[1:] + xy[:1]):
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


# --- providers ---------------------------------------------------------------------------------
class OverpassProvider:
    """Live OSM via Overpass, with ordered endpoint failover (trusted config only)."""

    name = "osm_overpass"
    live = True

    def __init__(self, http: HttpClient, endpoints: list[str], road_classes: list[str] | None = None,
                 timeout_sec: int = 60, maxsize_bytes: int = 64 * 1024 * 1024, snapshot_dir: str | None = None) -> None:
        if not endpoints:
            raise ValueError("at least one Overpass endpoint is required")
        self.http = http
        self.endpoints = list(endpoints)
        self.road_classes = list(road_classes or DEFAULT_ROAD_CLASSES)
        self.timeout = int(timeout_sec)
        self.maxsize = int(maxsize_bytes)
        self.snapshot_dir = Path(snapshot_dir) if snapshot_dir else None
        self._last: tuple[tuple, RawGeoData] | None = None

    def query(self, footprint: Footprint, venue: dict[str, Any]) -> str:
        return build_query(footprint, venue, self.road_classes, self.timeout, self.maxsize)

    def fetch(self, footprint: Footprint, venue: dict[str, Any]) -> RawGeoData:
        q = self.query(footprint, venue)
        errors = []
        started = time.perf_counter()
        for url in self.endpoints:
            try:
                payload = self.http.request_json("overpass", "POST", url,
                                                 body=("data=" + _urlquote(q)).encode("utf-8"),
                                                 headers={"Content-Type": "application/x-www-form-urlencoded"},
                                                 timeout=self.timeout + 10)
            except ProviderError as exc:
                errors.append(f"{_host(url)}: {exc.reason}")
                continue
            if isinstance(payload, dict) and payload.get("remark") and not payload.get("elements"):
                errors.append(f"{_host(url)}: {str(payload['remark'])[:160]}")
                continue
            raw = parse_overpass(payload, footprint, venue, self.road_classes)
            raw.meta.update({"provider": self.name, "endpoint": _host(url), "query_sha1": _sha1(q),
                             "fetch_ms": round((time.perf_counter() - started) * 1000.0, 1)})
            if self.snapshot_dir is not None:
                self._save_snapshot(q, payload)
            return raw
        raise ProviderError("overpass", "all Overpass endpoints failed: " + "; ".join(errors))

    def _save_snapshot(self, q: str, payload: Any) -> None:
        try:
            self.snapshot_dir.mkdir(parents=True, exist_ok=True)
            (self.snapshot_dir / f"{_sha1(q)}.json").write_text(json.dumps(payload), encoding="utf-8")
        except OSError:
            log.warning("could not write OSM snapshot")


class SnapshotProvider:
    """Replays a recorded Overpass response (OSM snapshot) — offline demos and tests.
    Reported as ``osm_snapshot``, never as live data."""

    name = "osm_snapshot"
    live = False

    def __init__(self, payload: Any, road_classes: list[str] | None = None) -> None:
        self.payload = payload
        self.road_classes = list(road_classes or DEFAULT_ROAD_CLASSES)

    @classmethod
    def from_file(cls, path: str | Path, road_classes: list[str] | None = None) -> "SnapshotProvider":
        return cls(json.loads(Path(path).read_text(encoding="utf-8")), road_classes)

    def fetch(self, footprint: Footprint, venue: dict[str, Any]) -> RawGeoData:
        raw = parse_overpass(self.payload, footprint, venue, self.road_classes)
        raw.meta.update({"provider": self.name, "endpoint": None, "query_sha1": None, "fetch_ms": 0.0})
        return raw


def _urlquote(s: str) -> str:
    import urllib.parse
    return urllib.parse.quote(s, safe="")


def _host(url: str) -> str:
    import urllib.parse
    return urllib.parse.urlparse(url).netloc


def _sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()
