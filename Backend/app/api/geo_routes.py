"""Venue → radius → footprint → blueprint → event graph endpoints.

    GET  /venues/search?q=            explicit venue search (never autocomplete)
    POST /venues/resolve              selection → normalised venue
    GET  /geospatial/status           providers, radius limits, active world
    POST /blueprints                  start a build (202; job with stages)
    GET  /blueprints/builds/{id}      build progress / result / structured error
    GET  /blueprints                  stored blueprints (headers)
    GET  /blueprints/{id}             full blueprint (nodes, edges, provenance)
    POST /blueprints/{id}/validate    validation report
    POST /blueprints/{id}/activate    make it the world the engine simulates (no restart)
    GET  /world                       the active world
    POST /world/synthetic-demo        switch back to the legacy synthetic demo world

Every response is a Pydantic model; raw provider responses never reach the wire.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Query

from .. import schemas as S
from ..errors import ApiError
from ..geospatial.blueprint import BlueprintError
from ..geospatial.footprint import RadiusError
from ..geospatial.http import ProviderError
from ..geospatial.service import get_blueprint_service
from ..geospatial.validation import validate_topology
from ..geospatial.venues import VenueError
from ..services.engine import get_engine
from ..ws.manager import MANAGER

log = logging.getLogger("eventflow.api.geo")
router = APIRouter(prefix="/api/v1")


@router.get("/venues/search", response_model=S.VenueSearchResponse)
async def venue_search(q: str = Query(..., max_length=200)) -> S.VenueSearchResponse:
    svc = get_blueprint_service()
    try:
        res = await asyncio.to_thread(svc.search, q)
    except VenueError as exc:
        raise ApiError("INVALID_VENUE", str(exc)) from exc
    return S.VenueSearchResponse(**res)


@router.post("/venues/resolve", response_model=S.Venue)
async def venue_resolve(body: S.VenueSelection) -> S.Venue:
    svc = get_blueprint_service()
    try:
        venue = await asyncio.to_thread(svc.resolver.resolve, body.model_dump(exclude_none=True))
    except VenueError as exc:
        raise ApiError("INVALID_VENUE", str(exc)) from exc
    except ProviderError as exc:
        raise ApiError("GEO_PROVIDER_UNAVAILABLE", f"Venue provider unavailable ({exc.reason}).",
                       {"provider": exc.provider}) from exc
    return S.Venue(**venue)


@router.get("/geospatial/status", response_model=S.GeospatialStatus)
async def geospatial_status() -> S.GeospatialStatus:
    return S.GeospatialStatus(**get_blueprint_service().status(), world=S.WorldInfo(**get_engine().world_info()))


@router.post("/blueprints", response_model=S.BlueprintJob, status_code=202)
async def start_build(body: S.BlueprintBuildRequest) -> S.BlueprintJob:
    svc = get_blueprint_service()
    try:
        job = svc.start_build(body.model_dump(exclude_none=True))
    except RadiusError as exc:
        raise ApiError("INVALID_RADIUS", str(exc), {"radius_m": body.radius_m}) from exc
    except VenueError as exc:
        raise ApiError("INVALID_VENUE", str(exc)) from exc
    return S.BlueprintJob(**job)


@router.get("/blueprints/builds/{build_id}", response_model=S.BlueprintJob)
async def build_status(build_id: str) -> S.BlueprintJob:
    job = get_blueprint_service().job_view(build_id)
    if job is None:
        raise ApiError("BUILD_NOT_FOUND", f"No blueprint build with id '{build_id}'.", {"build_id": build_id})
    return S.BlueprintJob(**job)


@router.get("/blueprints", response_model=S.BlueprintListResponse)
async def list_blueprints() -> S.BlueprintListResponse:
    svc = get_blueprint_service()
    active = get_engine().world.get("blueprint_id")
    items = await asyncio.to_thread(svc.list)
    return S.BlueprintListResponse(blueprints=[S.BlueprintHeader(**svc.header(b), active=b["blueprint_id"] == active)
                                               for b in items])


def _get(blueprint_id: str) -> dict:
    bp = get_blueprint_service().get(blueprint_id)
    if bp is None:
        raise ApiError("BLUEPRINT_NOT_FOUND", f"No blueprint with id '{blueprint_id}'.", {"blueprint_id": blueprint_id})
    return bp


@router.get("/blueprints/{blueprint_id}", response_model=S.Blueprint)
async def get_blueprint(blueprint_id: str) -> S.Blueprint:
    bp = _get(blueprint_id)
    svc = get_blueprint_service()
    return S.Blueprint(**svc.header(bp), active=get_engine().world.get("blueprint_id") == blueprint_id,
                       venue_geometry=bp.get("venue_geometry"), nodes=bp["nodes"], edges=bp["edges"],
                       zones=bp["zones"], properties=bp["properties"], bounds=bp["bounds"],
                       source_summary=bp["source_summary"])


@router.post("/blueprints/{blueprint_id}/validate", response_model=S.ValidationReport)
async def validate_blueprint(blueprint_id: str) -> S.ValidationReport:
    bp = _get(blueprint_id)
    report = await asyncio.to_thread(validate_topology, bp, require_provenance=True, properties=bp["properties"])
    return S.ValidationReport(**report)


async def _broadcast_world(engine) -> None:
    from .ws_routes import _resync_payload

    await MANAGER.broadcast("resync", _resync_payload(engine), engine.store.sim_time)


@router.post("/blueprints/{blueprint_id}/activate", response_model=S.WorldInfo)
async def activate_blueprint(blueprint_id: str, body: S.BlueprintActivateRequest) -> S.WorldInfo:
    bp = _get(blueprint_id)
    svc = get_blueprint_service()
    engine = get_engine()
    event = body.event.model_dump(exclude_none=True) if body.event else None
    try:
        world = svc.world_from_blueprint(bp, event)
        if event and (event.get("start_time") or event.get("end_time")):
            from ..services.events import EventSchedule
            ev = world["events"][0]
            ev["start_time"] = EventSchedule._utc(ev["start_time"], "start_time")
            ev["end_time"] = EventSchedule._utc(ev["end_time"], "end_time")
            if ev["end_time"] <= ev["start_time"]:
                raise ApiError("INVALID_SCHEDULE", "The event must end after it starts.")
        info = await engine.activate_world(world)
    except BlueprintError as exc:
        raise ApiError("BLUEPRINT_INVALID", exc.message, exc.detail) from exc
    except ApiError:
        raise
    except Exception as exc:
        log.exception("blueprint activation failed")
        raise ApiError("ACTIVATION_FAILED", "Blueprint could not be activated; the previous world is still running.",
                       {"blueprint_id": blueprint_id}) from exc
    await asyncio.to_thread(svc.save_active, "generated_blueprint", blueprint_id, event)
    await _broadcast_world(engine)
    return S.WorldInfo(**info)


@router.get("/world", response_model=S.WorldInfo)
async def world() -> S.WorldInfo:
    return S.WorldInfo(**get_engine().world_info())


@router.post("/world/synthetic-demo", response_model=S.WorldInfo)
async def activate_synthetic_demo() -> S.WorldInfo:
    engine = get_engine()
    info = await engine.activate_world(engine.legacy_world())
    await asyncio.to_thread(get_blueprint_service().save_active, engine.world["source"], None, None)
    await _broadcast_world(engine)
    return S.WorldInfo(**info)
