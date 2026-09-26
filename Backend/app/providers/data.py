"""Data providers: where the city's static data comes from, and the one place
external records are normalised into the wire conventions (00 §0).

    data:
      provider: synthetic          # default: generated topology, catalogue, config events
      # provider: file
      # dir: data/                 # events.json / hotels.json / topology.json (each optional)

A real feed (a ticketing export, a hotel inventory API, an OSM extract) plugs
in by producing the same shapes; `normalise_*` turns loosely formatted input
(mixed-case ids, local timestamps, rupees, percentages 0-100) into the
contract form and rejects what cannot be made valid, so nothing malformed ever
reaches the simulation. Any domain a file does not supply falls back to the
synthetic source, so partial real data still gives a working product.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger("eventflow.data")

ENTITY_TYPES = {"venue", "gate", "transport_node", "transport_route", "road", "zone", "hotel", "parking",
                "emergency_facility"}
TIERS = {"budget", "mid", "premium", "luxury"}


class DataError(ValueError):
    pass


# --- normalisation -----------------------------------------------------------------------------
def slug(value: Any) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", str(value).strip().lower()).strip("_")
    if not s:
        raise DataError(f"empty id from {value!r}")
    return s


def iso_utc(value: Any) -> str:
    """Any ISO 8601 timestamp -> 'YYYY-MM-DDTHH:MM:SSZ' in UTC (naive = UTC)."""
    text = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise DataError(f"bad timestamp {value!r}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fraction(value: Any) -> float:
    """0.0-1.0; a value above 1 is read as a percentage."""
    v = float(value)
    if v > 1.0:
        v /= 100.0
    if not 0.0 <= v <= 1.0:
        raise DataError(f"fraction out of range: {value!r}")
    return v


def paise(record: dict, key: str) -> int:
    if record.get(f"{key}_paise") is not None:
        return int(record[f"{key}_paise"])
    if record.get(f"{key}_inr") is not None:
        return int(round(float(record[f"{key}_inr"]) * 100))
    raise DataError(f"missing {key}_paise / {key}_inr")


def normalise_event(raw: dict, known_entities: set[str]) -> dict:
    ev = {
        "event_id": slug(raw.get("event_id") or raw.get("id") or raw["name"]),
        "name": str(raw["name"]).strip(),
        "category": str(raw.get("category", "event")).strip().lower(),
        "venue_entity_id": slug(raw["venue_entity_id"] if "venue_entity_id" in raw else raw["venue"]),
        "start_time": iso_utc(raw["start_time"]),
        "end_time": iso_utc(raw["end_time"]),
        "expected_attendance": int(raw["expected_attendance"]),
        "out_of_town_share": fraction(raw.get("out_of_town_share", 0.25)),
    }
    if ev["venue_entity_id"] not in known_entities:
        raise DataError(f"event {ev['event_id']}: unknown venue {ev['venue_entity_id']}")
    if ev["end_time"] <= ev["start_time"]:
        raise DataError(f"event {ev['event_id']}: ends before it starts")
    if ev["expected_attendance"] < 0:
        raise DataError(f"event {ev['event_id']}: negative attendance")
    return ev


def normalise_hotel(raw: dict, nodes: dict[str, dict]) -> dict:
    cluster = slug(raw["cluster_entity_id"])
    if nodes.get(cluster, {}).get("entity_type") != "hotel":
        raise DataError(f"hotel {raw.get('name')}: {cluster} is not a hotel cluster")
    tier = str(raw.get("tier", "mid")).strip().lower()
    if tier not in TIERS:
        raise DataError(f"hotel {raw.get('name')}: unknown tier {tier}")
    transport = slug(raw["transport_entity_id"]) if raw.get("transport_entity_id") else None
    if transport and transport not in nodes:
        raise DataError(f"hotel {raw.get('name')}: unknown transport {transport}")
    pid = slug(raw.get("property_id") or raw["name"])
    return {
        "property_id": pid if pid.startswith("htl_") else f"htl_{pid}",
        "name": str(raw["name"]).strip(),
        "cluster_entity_id": cluster,
        "zone": str(raw.get("zone") or nodes[cluster]["display_name"]).strip(),
        "lat": float(raw["lat"]),
        "lon": float(raw["lon"]),
        "rooms_total": int(raw["rooms_total"]),
        "price_per_night_paise": paise(raw, "price_per_night"),
        "tier": tier,
        "accessible": bool(raw.get("accessible", False)),
        "transport_entity_id": transport,
        "walk_to_transport_sec": int(raw.get("walk_to_transport_sec", 600)),
        "base_occupancy": fraction(raw.get("base_occupancy", 0.5)),
    }


def normalise_node(raw: dict) -> dict:
    etype = str(raw["entity_type"]).strip().lower()
    if etype not in ENTITY_TYPES:
        raise DataError(f"unknown entity_type {etype!r}")
    cap = float(raw["nominal_capacity"])
    if cap <= 0:
        raise DataError(f"{raw.get('entity_id')}: capacity must be positive")
    return {
        "entity_id": slug(raw["entity_id"]), "entity_type": etype,
        "display_name": str(raw.get("display_name") or raw["entity_id"]),
        "lat": float(raw["lat"]), "lon": float(raw["lon"]), "nominal_capacity": cap,
        "parent_id": slug(raw["parent_id"]) if raw.get("parent_id") else None, "meta": dict(raw.get("meta") or {}),
    }


# --- providers -----------------------------------------------------------------------------------
class DataProvider(Protocol):
    name: str

    def topology(self) -> dict[str, Any]: ...
    def properties(self, nodes: list[dict], edges: list[dict]) -> list[dict]: ...
    def events(self, configured: list[dict]) -> list[dict]: ...


class SyntheticData:
    name = "synthetic"

    def topology(self) -> dict[str, Any]:
        from ..topology import build_topology

        return build_topology()

    def properties(self, nodes, edges):
        from ..catalog import build_properties

        return build_properties(nodes, edges)

    def events(self, configured):
        return [dict(e) for e in configured]


class FileData(SyntheticData):
    """JSON files in one directory; each domain is optional (synthetic otherwise)."""

    name = "file"

    def __init__(self, directory: str | Path) -> None:
        self.dir = Path(directory)

    def _load(self, name: str) -> Any | None:
        p = self.dir / name
        if not p.exists():
            return None
        with open(p, encoding="utf-8") as f:
            return json.load(f)

    def topology(self):
        raw = self._load("topology.json")
        if raw is None:
            return super().topology()
        base = super().topology()
        nodes = [normalise_node(n) for n in raw["nodes"]]
        ids = {n["entity_id"] for n in nodes}
        edges = []
        for e in raw["edges"]:
            src, dst = slug(e["src_entity_id"]), slug(e["dst_entity_id"])
            if src not in ids or dst not in ids:
                raise DataError(f"edge {src}->{dst} references an unknown entity")
            edges.append({
                "edge_id": e.get("edge_id") or f"{src}__{dst}__{e['edge_type']}",
                "src_entity_id": src, "dst_entity_id": dst, "edge_type": e["edge_type"],
                "transfer_coefficient": float(e.get("transfer_coefficient", 0.0)),
                "travel_time_sec": int(e.get("travel_time_sec", 0)),
                "substitutability": float(e.get("substitutability", 0.0)),
            })
        lats, lons = [n["lat"] for n in nodes], [n["lon"] for n in nodes]
        return {"nodes": nodes, "edges": edges, "segments": raw.get("segments") or base["segments"],
                "bounds": {"min_lat": min(lats), "max_lat": max(lats), "min_lon": min(lons), "max_lon": max(lons)}}

    def properties(self, nodes, edges):
        raw = self._load("hotels.json")
        if raw is None:
            return super().properties(nodes, edges)
        by_id = {n["entity_id"]: n for n in nodes}
        return [normalise_hotel(h, by_id) for h in raw]

    def events(self, configured):
        raw = self._load("events.json")
        if raw is None:
            return super().events(configured)
        topo = self.topology()
        known = {n["entity_id"] for n in topo["nodes"]}
        return [normalise_event(e, known) for e in raw]


_PROVIDER: DataProvider | None = None


def build_data_provider(cfg: dict | None = None) -> DataProvider:
    cfg = cfg or {}
    if str(cfg.get("provider", "synthetic")).lower() == "file":
        directory = Path(cfg.get("dir", "data"))
        if not directory.is_absolute():
            directory = Path(__file__).resolve().parents[2] / directory
        return FileData(directory)
    return SyntheticData()


def get_data_provider() -> DataProvider:
    global _PROVIDER
    if _PROVIDER is None:
        from ..config import get_config

        _PROVIDER = build_data_provider(get_config().raw.get("data") or {})
    return _PROVIDER
