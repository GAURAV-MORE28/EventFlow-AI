"""Geospatial provider: travel time and distance between two points.

The product runs on synthetic geography by default (great-circle distance with
a street-network detour factor and per-mode speeds), which needs no network
and is fully deterministic. A real routing engine can be switched on without
code changes:

    EVENTFLOW_GEO_PROVIDER=osrm    OSRM_URL=http://localhost:5000
    EVENTFLOW_GEO_PROVIDER=google  GOOGLE_MAPS_API_KEY=...

Keys are read from the environment only (never from config files, never
logged). Every external call has a timeout, a bounded number of retries and a
result cache, and falls back to the synthetic estimate on any failure — the
answer's `source` says which one produced it, so a fallback is visible, not
silent. Routing on the simulation's critical path never waits on the network
for longer than the configured timeout.
"""
from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
import urllib.parse
import urllib.request
from typing import Any, Protocol

log = logging.getLogger("eventflow.geo")

EARTH_RADIUS_M = 6_371_000.0
# Street distance / straight-line distance in a dense city grid.
DETOUR_FACTOR = 1.3
# Door-to-door speeds (m/s) under event-day conditions.
SPEED_MPS = {"walk": 1.3, "drive": 6.5, "transit": 8.0, "cycle": 4.0}
MODES = tuple(SPEED_MPS)


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


class GeoProvider(Protocol):
    name: str

    def travel(self, origin: tuple[float, float], destination: tuple[float, float], mode: str = "walk") -> dict[str, Any]:
        """-> {"duration_sec": int, "distance_m": int, "source": str}"""
        ...


class SyntheticGeo:
    name = "synthetic"

    def travel(self, origin, destination, mode="walk"):
        dist = haversine_m(*origin, *destination) * DETOUR_FACTOR
        speed = SPEED_MPS.get(mode, SPEED_MPS["walk"])
        return {"duration_sec": int(round(dist / speed)), "distance_m": int(round(dist)), "source": self.name}


class _CachedRemote:
    """Shared plumbing for HTTP routing engines: cache, timeout, retries, fallback."""

    name = "remote"

    def __init__(self, timeout_sec: float = 2.0, retries: int = 1, cache_ttl_sec: float = 3600.0,
                 cache_size: int = 4096) -> None:
        self.timeout = timeout_sec
        self.retries = retries
        self.ttl = cache_ttl_sec
        self.size = cache_size
        self.fallback = SyntheticGeo()
        self._cache: dict[tuple, tuple[float, dict]] = {}
        self._lock = threading.Lock()
        self.stats = {"calls": 0, "cache_hits": 0, "failures": 0}

    def _key(self, origin, destination, mode) -> tuple:
        # ~10 m resolution: nearby requests share one lookup.
        return (round(origin[0], 4), round(origin[1], 4), round(destination[0], 4), round(destination[1], 4), mode)

    def travel(self, origin, destination, mode="walk"):
        key = self._key(origin, destination, mode)
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(key)
            if hit and now - hit[0] < self.ttl:
                self.stats["cache_hits"] += 1
                return dict(hit[1])
        result = None
        for attempt in range(self.retries + 1):
            try:
                self.stats["calls"] += 1
                result = self._fetch(origin, destination, mode)
                break
            except Exception as exc:  # network, HTTP, parse: all fall back
                self.stats["failures"] += 1
                log.warning("%s routing failed (attempt %d): %s", self.name, attempt + 1, type(exc).__name__)
                if attempt < self.retries:
                    time.sleep(min(0.2 * (2 ** attempt), 1.0))
        if result is None:
            out = self.fallback.travel(origin, destination, mode)
            out["source"] = f"synthetic_fallback:{self.name}"
            return out
        with self._lock:
            if len(self._cache) >= self.size:
                self._cache.pop(next(iter(self._cache)))
            self._cache[key] = (now, result)
        return dict(result)

    def _get_json(self, url: str) -> dict:
        req = urllib.request.Request(url, headers={"User-Agent": "EventFlow-AI"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310 (configured host)
            return json.loads(resp.read().decode("utf-8"))

    def _fetch(self, origin, destination, mode) -> dict:  # pragma: no cover - abstract
        raise NotImplementedError


class OSRMGeo(_CachedRemote):
    name = "osrm"
    PROFILE = {"walk": "foot", "drive": "driving", "cycle": "bike", "transit": "driving"}

    def __init__(self, base_url: str, **kw: Any) -> None:
        super().__init__(**kw)
        self.base_url = base_url.rstrip("/")

    def _fetch(self, origin, destination, mode):
        coords = f"{origin[1]},{origin[0]};{destination[1]},{destination[0]}"
        url = f"{self.base_url}/route/v1/{self.PROFILE.get(mode, 'foot')}/{coords}?overview=false"
        body = self._get_json(url)
        if body.get("code") != "Ok" or not body.get("routes"):
            raise ValueError(body.get("code", "no route"))
        r = body["routes"][0]
        return {"duration_sec": int(round(r["duration"])), "distance_m": int(round(r["distance"])), "source": self.name}


class GoogleGeo(_CachedRemote):
    name = "google"
    MODE = {"walk": "walking", "drive": "driving", "transit": "transit", "cycle": "bicycling"}
    URL = "https://maps.googleapis.com/maps/api/distancematrix/json"

    def __init__(self, api_key: str, **kw: Any) -> None:
        super().__init__(**kw)
        self._key_value = api_key

    def _fetch(self, origin, destination, mode):
        q = urllib.parse.urlencode({
            "origins": f"{origin[0]},{origin[1]}", "destinations": f"{destination[0]},{destination[1]}",
            "mode": self.MODE.get(mode, "walking"), "key": self._key_value,
        })
        body = self._get_json(f"{self.URL}?{q}")
        el = body["rows"][0]["elements"][0]
        if el.get("status") != "OK":
            raise ValueError(el.get("status", "no route"))
        return {"duration_sec": int(el["duration"]["value"]), "distance_m": int(el["distance"]["value"]),
                "source": self.name}


_PROVIDER: GeoProvider | None = None


def build_geo_provider(cfg: dict | None = None, env: dict | None = None) -> GeoProvider:
    cfg = cfg or {}
    env = os.environ if env is None else env
    kind = (env.get("EVENTFLOW_GEO_PROVIDER") or cfg.get("provider") or "synthetic").lower()
    kw = {
        "timeout_sec": float(cfg.get("timeout_sec", 2.0)),
        "retries": int(cfg.get("retries", 1)),
        "cache_ttl_sec": float(cfg.get("cache_ttl_sec", 3600)),
    }
    if kind == "osrm":
        url = env.get("OSRM_URL") or cfg.get("osrm_url")
        if url:
            return OSRMGeo(url, **kw)
        log.warning("geo provider 'osrm' selected but OSRM_URL is not set; using synthetic")
    elif kind == "google":
        key = env.get("GOOGLE_MAPS_API_KEY")
        if key:
            return GoogleGeo(key, **kw)
        log.warning("geo provider 'google' selected but GOOGLE_MAPS_API_KEY is not set; using synthetic")
    return SyntheticGeo()


def get_geo_provider() -> GeoProvider:
    global _PROVIDER
    if _PROVIDER is None:
        from ..config import get_config

        _PROVIDER = build_geo_provider(get_config().raw.get("geo") or {})
    return _PROVIDER
