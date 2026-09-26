"""The monitoring footprint: the region EventFlow builds and simulates.

The *monitoring* footprint (venue point + radius) is what drives data
acquisition. It is distinct from the *venue* footprint (the stadium polygon,
when OSM has one), which the blueprint keeps separately as venue geometry.

All distances are metres; all coordinates WGS84 degrees. Geometry is computed
on a sphere (haversine / destination point), deterministically, rounded to 7
decimals (~1 cm) so the same input always yields byte-identical output.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

EARTH_R = 6_371_000.0
RING_POINTS = 72


class RadiusError(ValueError):
    pass


def validate_radius(value: Any, min_m: float, max_m: float) -> float:
    """> 0, finite, within [min_m, max_m] — never silently clamped."""
    if isinstance(value, bool):
        raise RadiusError("Radius must be a number of metres.")
    try:
        r = float(value)
    except (TypeError, ValueError) as exc:
        raise RadiusError("Radius must be a number of metres.") from exc
    if not math.isfinite(r):
        raise RadiusError("Radius must be a finite number of metres.")
    if r <= 0:
        raise RadiusError("Radius must be greater than zero.")
    if r < min_m:
        raise RadiusError(f"Radius must be at least {int(min_m)} m.")
    if r > max_m:
        raise RadiusError(f"Radius is limited to {int(max_m)} m (larger areas make the OSM query and the "
                          "simulation too large for a live build).")
    return round(r, 1)


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R * math.asin(min(1.0, math.sqrt(a)))


def destination(lat: float, lon: float, bearing_deg: float, dist_m: float) -> tuple[float, float]:
    p1, l1, b, d = math.radians(lat), math.radians(lon), math.radians(bearing_deg), dist_m / EARTH_R
    p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(b))
    l2 = l1 + math.atan2(math.sin(b) * math.sin(d) * math.cos(p1), math.cos(d) - math.sin(p1) * math.sin(p2))
    return math.degrees(p2), (math.degrees(l2) + 540.0) % 360.0 - 180.0


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2, dl = math.radians(lat1), math.radians(lat2), math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


@dataclass(frozen=True)
class Footprint:
    center_lat: float
    center_lon: float
    radius_m: float

    @property
    def bounds(self) -> dict[str, float]:
        n = destination(self.center_lat, self.center_lon, 0.0, self.radius_m)[0]
        s = destination(self.center_lat, self.center_lon, 180.0, self.radius_m)[0]
        e = destination(self.center_lat, self.center_lon, 90.0, self.radius_m)[1]
        w = destination(self.center_lat, self.center_lon, 270.0, self.radius_m)[1]
        return {"min_lat": round(s, 7), "max_lat": round(n, 7), "min_lon": round(w, 7), "max_lon": round(e, 7)}

    def ring(self, points: int = RING_POINTS) -> list[list[float]]:
        """Closed [lon, lat] ring of the monitoring circle (for the map)."""
        out = []
        for i in range(points):
            lat, lon = destination(self.center_lat, self.center_lon, 360.0 * i / points, self.radius_m)
            out.append([round(lon, 7), round(lat, 7)])
        out.append(out[0])
        return out

    def contains(self, lat: float, lon: float, slack_m: float = 0.0) -> bool:
        return haversine_m(self.center_lat, self.center_lon, lat, lon) <= self.radius_m + slack_m

    def distance_m(self, lat: float, lon: float) -> float:
        return haversine_m(self.center_lat, self.center_lon, lat, lon)

    def to_dict(self) -> dict[str, Any]:
        return {
            "center_lat": round(self.center_lat, 7),
            "center_lon": round(self.center_lon, 7),
            "radius_m": self.radius_m,
            "bounds": self.bounds,
            "area_km2": round(math.pi * (self.radius_m / 1000.0) ** 2, 3),
            "ring": self.ring(),
        }


def make_footprint(lat: float, lon: float, radius_m: Any, min_m: float, max_m: float) -> Footprint:
    r = validate_radius(radius_m, min_m, max_m)
    return Footprint(round(float(lat), 7), round(float(lon), 7), r)
