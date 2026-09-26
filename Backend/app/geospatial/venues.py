"""Venue resolution: find a real venue and normalise its identity.

Providers (tried in configured order, each optional):

* ``coordinates``   — the organiser types "lat, lon". Always available, no network.
* ``nominatim``     — OSM Nominatim search. Server-side, explicit search only
                      (never autocomplete), throttled to 1 request/second, cached,
                      descriptive User-Agent, results attributed to OSM (ODbL).
* ``google_places`` — Places API (New) Text Search, only when an API key is set in
                      the environment. Discovery only: Google content is never
                      persisted except the ``place_id``; final coordinates are
                      re-resolved through OSM before anything is stored.

A normalised venue carries only what the application may use and keep::

    venue_id, display_name, formatted_address, lat, lon, source, source_id,
    confidence, category, osm_type, osm_id, bbox, google_place_id, persistable
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
from typing import Any, Protocol

from .http import HttpClient, ProviderError

log = logging.getLogger("eventflow.geospatial.venues")

COORD_RE = re.compile(r"^\s*(-?\d{1,3}(?:\.\d+)?)\s*[,;\s]\s*(-?\d{1,3}(?:\.\d+)?)\s*$")
# OSM categories that describe a place an event can be held at (for ranking only).
VENUE_CLASSES = {
    ("leisure", "stadium"), ("leisure", "sports_centre"), ("leisure", "pitch"), ("leisure", "park"),
    ("amenity", "arts_centre"), ("amenity", "theatre"), ("amenity", "exhibition_centre"),
    ("amenity", "conference_centre"), ("amenity", "events_venue"), ("building", "stadium"),
    ("tourism", "attraction"),
}
OSM_TYPE = {"node": "N", "way": "W", "relation": "R", "N": "N", "W": "W", "R": "R"}
OSM_TYPE_NAME = {"N": "node", "W": "way", "R": "relation"}


class VenueError(ValueError):
    """The organiser's venue query or selection is invalid (not a provider failure)."""


def venue_id_for(source: str, source_id: str) -> str:
    return "v_" + hashlib.sha1(f"{source}|{source_id}".encode()).hexdigest()[:12]


def valid_coordinates(lat: Any, lon: Any) -> tuple[float, float]:
    try:
        la, lo = float(lat), float(lon)
    except (TypeError, ValueError) as exc:
        raise VenueError("Coordinates must be numbers.") from exc
    if not (math.isfinite(la) and math.isfinite(lo)) or not (-90 <= la <= 90) or not (-180 <= lo <= 180):
        raise VenueError("Coordinates must be finite, latitude in [-90, 90] and longitude in [-180, 180].")
    return round(la, 7), round(lo, 7)


def clean_query(query: Any) -> str:
    q = re.sub(r"\s+", " ", str(query or "")).strip()
    if len(q) < 3:
        raise VenueError("Enter at least 3 characters to search for a venue.")
    if len(q) > 200:
        raise VenueError("Venue search text is limited to 200 characters.")
    return q


def _venue(source: str, source_id: str, name: str, lat: float, lon: float, *, address: str | None = None,
           confidence: str = "medium", category: str | None = None, osm_type: str | None = None,
           osm_id: int | None = None, bbox: list[float] | None = None, place_id: str | None = None,
           persistable: bool = True, capacity: int | None = None) -> dict[str, Any]:
    return {
        "venue_id": venue_id_for(source, source_id),
        "display_name": name,
        "formatted_address": address,
        "lat": lat,
        "lon": lon,
        "source": source,
        "source_id": source_id,
        "confidence": confidence,
        "category": category,
        "osm_type": osm_type,
        "osm_id": osm_id,
        "bbox": bbox,
        "google_place_id": place_id,
        "persistable": persistable,
        "osm_capacity": capacity,
    }


class VenueProvider(Protocol):
    name: str

    def available(self) -> bool: ...
    def search(self, query: str, limit: int) -> list[dict[str, Any]]: ...


class CoordinateVenueProvider:
    """"19.07, 72.87" → a venue at that point. The organiser is the source."""

    name = "coordinates"

    def available(self) -> bool:
        return True

    def search(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        m = COORD_RE.match(query)
        if not m:
            return []
        lat, lon = valid_coordinates(m.group(1), m.group(2))
        return [self.at(lat, lon)]

    @staticmethod
    def at(lat: float, lon: float, name: str | None = None) -> dict[str, Any]:
        lat, lon = valid_coordinates(lat, lon)
        label = name.strip() if name and name.strip() else f"Location {lat:.5f}, {lon:.5f}"
        return _venue("coordinates", f"{lat:.7f},{lon:.7f}", label[:120], lat, lon, confidence="high",
                      category="organizer_point")


def _nominatim_confidence(item: dict) -> str:
    cls = (item.get("category") or item.get("class"), item.get("type"))
    if cls in VENUE_CLASSES:
        return "high"
    return "medium" if float(item.get("importance") or 0.0) >= 0.3 else "low"


class NominatimVenueProvider:
    """OSM Nominatim — explicit, throttled, cached search. Never used for autocomplete
    and never for POI harvesting (that is the Overpass provider's focused job)."""

    name = "nominatim"

    def __init__(self, http: HttpClient, base_url: str) -> None:
        self.http = http
        self.base_url = base_url.rstrip("/")

    def available(self) -> bool:
        return True

    @staticmethod
    def normalise(item: dict) -> dict[str, Any] | None:
        try:
            lat, lon = valid_coordinates(item["lat"], item["lon"])
        except (KeyError, VenueError):
            return None
        otype = OSM_TYPE.get(str(item.get("osm_type", "")).lower()) or OSM_TYPE.get(str(item.get("osm_type", "")))
        oid = item.get("osm_id")
        if otype is None or not isinstance(oid, int):
            return None
        name = (item.get("name") or str(item.get("display_name", "")).split(",")[0]).strip()
        if not name:
            return None
        bbox = None
        if isinstance(item.get("boundingbox"), list) and len(item["boundingbox"]) == 4:
            try:
                s, n, w, e = (float(x) for x in item["boundingbox"])
                bbox = [round(s, 7), round(w, 7), round(n, 7), round(e, 7)]
            except (TypeError, ValueError):
                bbox = None
        cap = None
        extratags = item.get("extratags") or {}
        if str(extratags.get("capacity", "")).isdigit():
            cap = int(extratags["capacity"])
        cls = item.get("category") or item.get("class")
        return _venue("nominatim", f"{OSM_TYPE_NAME[otype]}/{oid}", name[:120], lat, lon,
                      address=str(item.get("display_name") or "")[:300] or None,
                      confidence=_nominatim_confidence(item), category=f"{cls}={item.get('type')}" if cls else None,
                      osm_type=OSM_TYPE_NAME[otype], osm_id=oid, bbox=bbox, capacity=cap)

    def search(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        data = self.http.request_json("nominatim", "GET", f"{self.base_url}/search", params={
            "q": query, "format": "jsonv2", "limit": max(1, min(limit, 10)), "extratags": 1,
        })
        if not isinstance(data, list):
            raise ProviderError("nominatim", "unexpected response shape")
        out = [v for v in (self.normalise(x) for x in data if isinstance(x, dict)) if v]
        return _dedupe(out)

    def lookup(self, osm_type: str, osm_id: int) -> dict[str, Any] | None:
        code = OSM_TYPE.get(osm_type)
        if code is None:
            raise VenueError(f"Unknown OSM type {osm_type!r}.")
        data = self.http.request_json("nominatim", "GET", f"{self.base_url}/lookup", params={
            "osm_ids": f"{code}{int(osm_id)}", "format": "jsonv2", "extratags": 1,
        })
        if not isinstance(data, list) or not data:
            return None
        return self.normalise(data[0])

    def near(self, name: str, lat: float, lon: float) -> dict[str, Any] | None:
        """The OSM object for a named place close to a point (used to re-resolve a
        Google discovery into persistable OSM identity)."""
        d = 0.02
        data = self.http.request_json("nominatim", "GET", f"{self.base_url}/search", params={
            "q": name, "format": "jsonv2", "limit": 5, "extratags": 1, "bounded": 1,
            "viewbox": f"{lon - d},{lat + d},{lon + d},{lat - d}",
        })
        best = None
        for item in data if isinstance(data, list) else []:
            v = self.normalise(item)
            if v is None:
                continue
            dist = haversine_m(lat, lon, v["lat"], v["lon"])
            if dist <= 1500 and (best is None or dist < best[0]):
                best = (dist, v)
        return best[1] if best else None


class GooglePlacesVenueProvider:
    """Places API (New) Text Search with a field mask. Discovery only."""

    name = "google_places"
    URL = "https://places.googleapis.com/v1/places:searchText"
    FIELD_MASK = "places.id,places.displayName,places.formattedAddress,places.location"

    def __init__(self, http: HttpClient, api_key: str | None) -> None:
        self.http = http
        self._key = api_key

    def available(self) -> bool:
        return bool(self._key)

    def search(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        if not self._key:
            return []
        body = json.dumps({"textQuery": query, "pageSize": max(1, min(limit, 10))}).encode("utf-8")
        data = self.http.request_json("google_places", "POST", self.URL, body=body, cache=False, headers={
            "Content-Type": "application/json", "X-Goog-Api-Key": self._key, "X-Goog-FieldMask": self.FIELD_MASK,
        })
        out = []
        for p in (data or {}).get("places", []) if isinstance(data, dict) else []:
            try:
                lat, lon = valid_coordinates(p["location"]["latitude"], p["location"]["longitude"])
                pid = str(p["id"])
                name = str(p.get("displayName", {}).get("text") or "").strip()
            except (KeyError, TypeError, VenueError):
                continue
            if not name:
                continue
            out.append(_venue("google_places", pid, name[:120], lat, lon, address=p.get("formattedAddress"),
                              confidence="medium", category="google_place", place_id=pid, persistable=False))
        return out


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6_371_000.0 * math.asin(min(1.0, math.sqrt(a)))


def _dedupe(venues: list[dict]) -> list[dict]:
    seen, out = set(), []
    for v in venues:
        if v["venue_id"] in seen:
            continue
        seen.add(v["venue_id"])
        out.append(v)
    return out


class VenueResolver:
    """Tries the configured providers in order and keeps a short-lived index of
    results so a selection can be resolved without another external request."""

    def __init__(self, providers: list[Any], nominatim: NominatimVenueProvider | None = None) -> None:
        self.providers = providers
        self.nominatim = nominatim
        self._seen: dict[str, dict[str, Any]] = {}

    def status(self) -> list[dict[str, Any]]:
        return [{"provider": p.name, "available": p.available()} for p in self.providers]

    def search(self, query: str, limit: int = 8) -> dict[str, Any]:
        q = clean_query(query)
        results: list[dict] = []
        attempts: list[dict[str, Any]] = []
        for p in self.providers:
            if not p.available():
                attempts.append({"provider": p.name, "status": "not_configured"})
                continue
            try:
                found = p.search(q, limit)
            except VenueError:
                raise
            except ProviderError as exc:
                attempts.append({"provider": p.name, "status": "failed", "reason": exc.reason})
                continue
            attempts.append({"provider": p.name, "status": "ok", "results": len(found)})
            if found:
                results = found[:limit]
                break
        for v in results:
            self._seen[v["venue_id"]] = v
        while len(self._seen) > 500:
            self._seen.pop(next(iter(self._seen)))
        failed = [a for a in attempts if a["status"] == "failed"]
        return {"query": q, "results": results, "attempts": attempts,
                "provider": results[0]["source"] if results else None,
                "all_failed": bool(failed) and not results and all(
                    a["status"] != "ok" for a in attempts if a["provider"] != "coordinates")}

    def resolve(self, selection: dict[str, Any]) -> dict[str, Any]:
        """Selection → normalised venue. Accepts a venue_id from a previous search,
        an OSM reference, or organiser coordinates (+ optional name)."""
        vid = selection.get("venue_id")
        if vid and vid in self._seen:
            venue = dict(self._seen[vid])
        elif selection.get("source") == "nominatim" and selection.get("osm_type") and selection.get("osm_id"):
            if self.nominatim is None:
                raise ProviderError("nominatim", "not configured")
            venue = self.nominatim.lookup(str(selection["osm_type"]), int(selection["osm_id"]))
            if venue is None:
                raise VenueError("That OSM object could not be found.")
        elif selection.get("lat") is not None and selection.get("lon") is not None:
            venue = CoordinateVenueProvider.at(selection["lat"], selection["lon"], selection.get("display_name"))
        else:
            raise VenueError("Select a venue from the search results or enter coordinates.")
        if venue["source"] == "google_places":
            # Google content may not be stored: re-resolve to an OSM identity near the
            # discovered point and keep only the place_id from Google.
            osm = None
            if self.nominatim is not None:
                try:
                    osm = self.nominatim.near(venue["display_name"], venue["lat"], venue["lon"])
                except ProviderError:
                    osm = None
            if osm is not None:
                osm["google_place_id"] = venue["google_place_id"]
                venue = osm
        return venue


def build_resolver(cfg: dict[str, Any], http: HttpClient, env: dict | None = None) -> VenueResolver:
    env = os.environ if env is None else env
    order = cfg.get("venue_providers") or ["coordinates", "google_places", "nominatim"]
    nominatim = NominatimVenueProvider(http, cfg.get("nominatim_url", "https://nominatim.openstreetmap.org"))
    registry = {
        "coordinates": CoordinateVenueProvider(),
        "nominatim": nominatim,
        "google_places": GooglePlacesVenueProvider(http, env.get("GOOGLE_PLACES_API_KEY") or env.get("GOOGLE_MAPS_API_KEY")),
    }
    providers = [registry[p] for p in order if p in registry]
    return VenueResolver(providers, nominatim if "nominatim" in order else None)
