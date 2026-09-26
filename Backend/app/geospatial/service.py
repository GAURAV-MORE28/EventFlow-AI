"""Blueprint service: venue search, build jobs, persistence, activation.

A build runs as a background job with named stages (resolving venue → footprint →
acquiring OSM → building blueprint → validating), each timed, so the UI can show
real progress. A blueprint becomes the engine's world only on explicit
activation; building never changes the running simulation.

Persistence: the *normalised* blueprint (OSM-derived, ODbL-attributed) is stored,
never raw provider responses. A blueprint whose venue came from Google Places and
could not be re-resolved to OSM is kept in memory only (Google content may not be
stored; only its place_id may).
"""
from __future__ import annotations

import copy
import hashlib
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any

from ..config import get_config
from .blueprint import BlueprintBuilder, BlueprintError
from .footprint import RadiusError, make_footprint
from .http import HttpClient, ProviderError
from .overpass import ATTRIBUTION, DEFAULT_ROAD_CLASSES, OverpassProvider
from .validation import validate_topology
from .venues import VenueError, build_resolver

log = logging.getLogger("eventflow.geospatial")

STAGES = ["resolving_venue", "footprint", "acquiring_osm", "building_blueprint", "validating"]
MEMORY_LIMIT = 12


class BlueprintService:
    def __init__(self, cfg: dict[str, Any] | None = None, provider: Any | None = None,
                 http: HttpClient | None = None) -> None:
        raw = get_config().raw
        self.cfg = dict(cfg if cfg is not None else raw.get("geospatial") or {})
        g = self.cfg
        hosts = {"nominatim.openstreetmap.org": float(g.get("nominatim_min_interval_sec", 1.1))}
        for url in g.get("overpass_endpoints") or []:
            from urllib.parse import urlparse
            hosts[urlparse(url).netloc] = float(g.get("overpass_min_interval_sec", 1.0))
        self.http = http or HttpClient(g.get("user_agent", "EventFlow-AI/1.0"), timeout_sec=float(g.get("timeout_sec", 60)),
                                       retries=int(g.get("retries", 1)), min_interval_sec=hosts,
                                       cache_ttl_sec=float(g.get("cache_ttl_sec", 3600)))
        self.resolver = build_resolver(g, self.http)
        self.road_classes = list(g.get("road_classes") or DEFAULT_ROAD_CLASSES)
        self.provider = provider or OverpassProvider(self.http, list(g.get("overpass_endpoints") or []),
                                                     self.road_classes, int(g.get("timeout_sec", 60)))
        from ..topology import SEGMENTS
        self.builder = BlueprintBuilder(dict(g.get("builder") or {}), SEGMENTS)
        self.radius_min = float(g.get("radius_min_m", 250))
        self.radius_max = float(g.get("radius_max_m", 5000))
        self.radius_default = float(g.get("radius_default_m", 2000))
        self._blueprints: dict[str, dict[str, Any]] = {}
        self._jobs: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._counter = 0

    # --- venues ------------------------------------------------------------------------------------------
    def search(self, query: str) -> dict[str, Any]:
        res = self.resolver.search(query)
        res["attribution"] = self.attribution(res["results"])
        return res

    @staticmethod
    def attribution(venues: list[dict]) -> list[str]:
        out = []
        if any(v["source"] == "nominatim" for v in venues):
            out.append("Search: Nominatim, " + ATTRIBUTION)
        if any(v["source"] == "google_places" for v in venues):
            out.append("Google Maps")
        return out

    def status(self) -> dict[str, Any]:
        from urllib.parse import urlparse
        return {
            "venue_providers": self.resolver.status(),
            "geo_provider": self.provider.name, "geo_live": bool(getattr(self.provider, "live", False)),
            "overpass_endpoints": [urlparse(u).netloc for u in getattr(self.provider, "endpoints", [])],
            "radius_min_m": self.radius_min, "radius_max_m": self.radius_max, "radius_default_m": self.radius_default,
            "attribution": [ATTRIBUTION],
        }

    # --- building --------------------------------------------------------------------------------------------
    def validate_request(self, request: dict[str, Any]) -> None:
        """Synchronous checks (400s) before a job is started."""
        make_footprint(0.0, 0.0, request.get("radius_m"), self.radius_min, self.radius_max)
        sel = request.get("venue") or {}
        if not (sel.get("venue_id") or (sel.get("lat") is not None and sel.get("lon") is not None)
                or (sel.get("source") == "nominatim" and sel.get("osm_type") and sel.get("osm_id"))):
            raise VenueError("Select a venue from the search results or enter coordinates.")
        if sel.get("lat") is not None or sel.get("lon") is not None:
            from .venues import valid_coordinates
            valid_coordinates(sel.get("lat"), sel.get("lon"))

    def build(self, request: dict[str, Any], progress=None) -> dict[str, Any]:
        """Venue + radius → validated blueprint (synchronous; jobs call this in a thread)."""
        timings: dict[str, float] = {}

        def stage(name: str):
            if progress:
                progress(name)
            return time.perf_counter()
        t = stage("resolving_venue")
        venue = self.resolver.resolve(request["venue"])
        timings["resolving_venue"] = _ms(t)
        t = stage("footprint")
        fp = make_footprint(venue["lat"], venue["lon"], request["radius_m"], self.radius_min, self.radius_max)
        timings["footprint"] = _ms(t)
        t = stage("acquiring_osm")
        raw = self.provider.fetch(fp, venue)
        raw.meta["road_classes"] = self.road_classes
        timings["acquiring_osm"] = _ms(t)
        t = stage("building_blueprint")
        data_source = "live_osm" if getattr(self.provider, "live", False) else "osm_snapshot"
        bp = self.builder.build(venue, fp, raw, venue_capacity=request.get("venue_capacity"),
                                max_gates=request.get("max_gates"), data_source=data_source)
        timings["building_blueprint"] = _ms(t)
        t = stage("validating")
        bp["validation"] = validate_topology(bp, require_provenance=True, properties=bp["properties"])
        timings["validating"] = _ms(t)
        bp["metadata"]["timings_ms"] = timings
        self._remember(bp)
        return bp

    def start_build(self, request: dict[str, Any]) -> dict[str, Any]:
        self.validate_request(request)
        with self._lock:
            self._counter += 1
            bid = "build_" + hashlib.sha1(f"{self._counter}|{time.time_ns()}".encode()).hexdigest()[:10]
            job = {"build_id": bid, "status": "running", "stage": "queued",
                   "stages": [{"stage": s, "status": "pending"} for s in STAGES],
                   "error": None, "blueprint_id": None, "header": None}
            self._jobs[bid] = job
            while len(self._jobs) > 50:
                self._jobs.pop(next(iter(self._jobs)))
        threading.Thread(target=self._run_job, args=(job, copy.deepcopy(request)), daemon=True,
                         name=f"blueprint-{bid}").start()
        return self.job_view(bid)

    def _run_job(self, job: dict[str, Any], request: dict[str, Any]) -> None:
        started = {}

        def progress(name: str) -> None:
            now = time.perf_counter()
            for st in job["stages"]:
                if st["status"] == "running":
                    st["status"] = "done"
                    st["ms"] = round((now - started[st["stage"]]) * 1000.0, 1)
                if st["stage"] == name:
                    st["status"] = "running"
            started[name] = now
            job["stage"] = name
        try:
            bp = self.build(request, progress)
        except (VenueError, RadiusError) as exc:
            self._fail(job, "INVALID_REQUEST", str(exc))
        except ProviderError as exc:
            self._fail(job, "GEO_PROVIDER_UNAVAILABLE",
                       f"Live geospatial data is unavailable ({exc.reason}). The current world was not changed.",
                       {"provider": exc.provider})
        except BlueprintError as exc:
            self._fail(job, exc.code, exc.message, exc.detail)
        except Exception:
            log.exception("blueprint build failed")
            self._fail(job, "INTERNAL_ERROR", "The blueprint build failed unexpectedly.")
        else:
            progress("done")
            job.update({"status": "complete", "stage": "done", "blueprint_id": bp["blueprint_id"],
                        "header": self.header(bp)})

    @staticmethod
    def _fail(job: dict, code: str, message: str, detail: dict | None = None) -> None:
        for st in job["stages"]:
            if st["status"] == "running":
                st["status"] = "failed"
        job.update({"status": "failed", "error": {"code": code, "message": message, "detail": detail or {}}})

    def job_view(self, build_id: str) -> dict[str, Any] | None:
        job = self._jobs.get(build_id)
        return copy.deepcopy(job) if job else None

    # --- storage -------------------------------------------------------------------------------------------------
    def _remember(self, bp: dict[str, Any]) -> None:
        with self._lock:
            self._blueprints.pop(bp["blueprint_id"], None)
            self._blueprints[bp["blueprint_id"]] = bp
            while len(self._blueprints) > MEMORY_LIMIT:
                self._blueprints.pop(next(iter(self._blueprints)))
        if bp.get("persistable", True):
            self._persist(bp)

    def _persist(self, bp: dict[str, Any]) -> None:
        from ..db import models
        from ..db.base import SessionLocal
        try:
            with SessionLocal() as session:
                session.merge(models.BlueprintRecord(
                    blueprint_id=bp["blueprint_id"], graph_hash=bp["graph_hash"],
                    venue_name=bp["venue"]["display_name"], venue_source=bp["venue"]["source"],
                    radius_m=float(bp["footprint"]["radius_m"]), data_source=bp["metadata"]["data_source"],
                    created_at=datetime.now(timezone.utc), payload=_storable(bp)))
                session.commit()
        except Exception:
            log.exception("blueprint persistence failed (kept in memory)")

    def get(self, blueprint_id: str) -> dict[str, Any] | None:
        bp = self._blueprints.get(blueprint_id)
        if bp is not None:
            return bp
        from ..db import models
        from ..db.base import SessionLocal
        try:
            with SessionLocal() as session:
                rec = session.get(models.BlueprintRecord, blueprint_id)
                if rec is not None:
                    bp = copy.deepcopy(rec.payload)
                    with self._lock:
                        self._blueprints[blueprint_id] = bp
                    return bp
        except Exception:
            log.exception("blueprint lookup failed")
        return None

    def list(self) -> list[dict[str, Any]]:
        seen: dict[str, dict] = {}
        from ..db import models
        from ..db.base import SessionLocal
        try:
            with SessionLocal() as session:
                for rec in session.query(models.BlueprintRecord).order_by(models.BlueprintRecord.created_at.desc()).limit(20):
                    seen[rec.blueprint_id] = rec.payload
        except Exception:
            log.exception("blueprint listing failed")
        for bid, bp in self._blueprints.items():
            seen[bid] = bp
        return sorted(seen.values(), key=lambda b: b.get("generated_at", ""), reverse=True)

    @staticmethod
    def header(bp: dict[str, Any]) -> dict[str, Any]:
        keys = ("blueprint_id", "graph_hash", "venue", "venue_entity_id", "footprint", "summary", "warnings",
                "provenance_summary", "metadata", "validation", "persistable", "generated_at")
        out = {k: bp[k] for k in keys}
        out["radius_m"] = bp["footprint"]["radius_m"]
        return out

    # --- activation ------------------------------------------------------------------------------------------
    def world_from_blueprint(self, bp: dict[str, Any], event: dict[str, Any] | None = None) -> dict[str, Any]:
        if not bp.get("validation", {}).get("valid", False):
            raise BlueprintError("BLUEPRINT_INVALID", "Blueprint could not be activated: "
                                 + "; ".join(bp.get("validation", {}).get("errors") or ["validation failed"]))
        raw = get_config().raw
        primary = raw["event"]
        venue_node = next(n for n in bp["nodes"] if n["entity_id"] == bp["venue_entity_id"])
        ev = dict(event or {})
        attendance = ev.get("expected_attendance")
        attendance_default = attendance is None
        if attendance is None:
            attendance = int(round(venue_node["nominal_capacity"] * 0.85))
        event_id = "evt_" + bp["graph_hash"][:10]
        schedule = [{
            "event_id": event_id,
            "name": ev.get("name") or f"Event at {bp['venue']['display_name']}"[:120],
            "category": ev.get("category") or "event",
            "venue_entity_id": bp["venue_entity_id"],
            "start_time": ev.get("start_time") or primary["start_time"],
            "end_time": ev.get("end_time") or primary["end_time"],
            "expected_attendance": int(attendance),
            "out_of_town_share": float(ev.get("out_of_town_share", 0.25)),
            "description": ("Attendance defaulted to 85% of the venue's " + venue_node["capacity_source"]
                            + " capacity." if attendance_default else None),
        }]
        topology = {"nodes": copy.deepcopy(bp["nodes"]), "edges": copy.deepcopy(bp["edges"]),
                    "segments": copy.deepcopy(bp["segments"]), "bounds": dict(bp["bounds"]),
                    "properties": copy.deepcopy(bp["properties"]), "defaults": {}, "source": "generated_blueprint"}
        return {
            "world_id": bp["blueprint_id"], "source": "generated_blueprint",
            "data_source": bp["metadata"]["data_source"], "blueprint_id": bp["blueprint_id"],
            "graph_hash": bp["graph_hash"], "topology": topology, "events": schedule,
            "primary_event_id": event_id, "venue_entity_id": bp["venue_entity_id"],
            "footprint": bp["footprint"], "venue_geometry": bp.get("venue_geometry"),
            "gnn_supported": False, "attribution": bp["metadata"]["attribution"],
        }

    @staticmethod
    def save_active(source: str, blueprint_id: str | None, event: dict | None) -> None:
        from ..db import models
        from ..db.base import SessionLocal
        try:
            with SessionLocal() as session:
                session.merge(models.ActiveWorld(key="active", source=source, blueprint_id=blueprint_id, event=event,
                                                 updated_at=datetime.now(timezone.utc)))
                session.commit()
        except Exception:
            log.exception("active world persistence failed")

    @staticmethod
    def load_active() -> dict[str, Any] | None:
        from ..db import models
        from ..db.base import SessionLocal
        try:
            with SessionLocal() as session:
                rec = session.get(models.ActiveWorld, "active")
                if rec is None:
                    return None
                return {"source": rec.source, "blueprint_id": rec.blueprint_id, "event": rec.event}
        except Exception:
            log.exception("active world lookup failed")
            return None


def _storable(bp: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(bp)
    if out["venue"].get("source") == "google_places":     # never persist Google content (place_id only)
        out["venue"] = {**out["venue"], "display_name": "Venue", "formatted_address": None}
    return out


def _ms(t: float) -> float:
    return round((time.perf_counter() - t) * 1000.0, 1)


_SERVICE: BlueprintService | None = None


def get_blueprint_service() -> BlueprintService:
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = BlueprintService()
    return _SERVICE


def set_blueprint_service(service: BlueprintService | None) -> None:
    global _SERVICE
    _SERVICE = service
