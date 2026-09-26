"""REST surface — 01_BACKEND_CONTRACT.md §3, in contract order.

Every handler returns a Pydantic model, so nothing reaches the wire without
being validated against 00_SHARED_CONTRACT.md first. Read paths prefer the cache
(01 §5) and fall back to the live store.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Query

from .. import schemas as S
from ..cache import CACHE, TTL
from ..config import get_config
from ..errors import ApiError
from ..ml_reference.common import band_from_score
from ..services import accommodation as ACC
from ..services.attendee import apply_nudge_response, build_journey, issue_nudges, public_nudge
from ..services.engine import get_engine
from ..services.metrics import build_metrics, build_regret
from ..services.simulation import SIMULATIONS
from ..simtime import iso, parse, server_now
from ..ws.manager import MANAGER

log = logging.getLogger("eventflow.api")
router = APIRouter(prefix="/api/v1")


# --- §3.1 system ------------------------------------------------------------
@router.get("/health", response_model=S.HealthResponse)
async def health() -> S.HealthResponse:
    engine = get_engine()
    store = engine.store
    registry = engine.registry
    twin_ready = registry.twin.ready() if hasattr(registry.twin, "ready") else True

    return S.HealthResponse(
        status="ok",
        server_time=server_now(),
        sim_time=store.sim_time,
        cycle_number=store.cycle_number,
        modules={
            "forecaster": S.ModuleHealth(
                ready=registry.forecaster.ready(), active_source=store.active_forecast_source
            ),
            "cascade": S.ModuleHealth(
                ready=registry.cascade.ready(), active_source=store.active_cascade_source
            ),
            "twin": S.ModuleHealth(
                ready=twin_ready,
                ensemble_size=int(get_config().raw["twin"]["ensemble_size"]),
            ),
            "equilibrium": S.ModuleHealth(ready=registry.equilibrium.ready()),
            "commander": S.ModuleHealth(ready=True, active_source=engine.commander.engine_mode),
        },
    )


# --- §3.2 topology ----------------------------------------------------------
@router.get("/event", response_model=S.EventResponse)
async def event() -> S.EventResponse:
    engine = get_engine()
    primary = engine.events.get(engine.events.primary_event_id)
    concurrent = {e["event_id"] for e in engine.events.concurrent_with(primary["event_id"])}
    return S.EventResponse(
        event_id=primary["event_id"],
        name=primary["name"],
        venue_entity_id=primary["venue_entity_id"],
        expected_attendance=int(primary["expected_attendance"]),
        start_time=primary["start_time"],
        end_time=primary["end_time"],
        sim_time=engine.store.sim_time,
        concurrent_events=[v for v in engine.event_views() if v["event_id"] in concurrent],
    )


@router.get("/graph", response_model=S.GraphResponse)
async def graph() -> S.GraphResponse:
    store = get_engine().store
    return S.GraphResponse(
        nodes=[S.Entity(**n) for n in store.nodes.values()],
        edges=[S.GraphEdge(**e) for e in store.edges],
        segments=[S.Segment(**s) for s in store.segments],
        bounds=S.Bounds(**store.bounds),
    )


# --- §3.3 live state ---------------------------------------------------------
@router.get("/state", response_model=S.StateResponse)
async def state() -> S.StateResponse:
    cached = CACHE.get("state:current")
    payload = cached or get_engine().state_payload()
    return S.StateResponse(**payload)


@router.get("/state/{entity_id}", response_model=S.EntityDetailResponse)
async def entity_detail(entity_id: str) -> S.EntityDetailResponse:
    store = get_engine().store
    st = store.entity_states.get(entity_id)
    if st is None:
        if entity_id not in store.nodes:
            raise ApiError("ENTITY_NOT_FOUND", f"No entity with id '{entity_id}'.", {"entity_id": entity_id})
        raise ApiError("MODEL_NOT_READY", "State not yet available for this entity.", {"entity_id": entity_id})

    forecast = store.forecasts.get(entity_id)
    breakdown = store.risk_breakdown.get(entity_id) or [
        {"risk_type": "overall", "score": st["risk_score"]}
    ]
    return S.EntityDetailResponse(
        state=S.EntityState(**st),
        forecast=S.Forecast(**forecast) if forecast else None,
        edges_in=[S.GraphEdge(**e) for e in store.edges_by_dst.get(entity_id, [])],
        edges_out=[S.GraphEdge(**e) for e in store.edges_by_src.get(entity_id, [])],
        risk_breakdown=[S.RiskBreakdownItem(**b) for b in breakdown],
    )


# --- §3.4 forecast ------------------------------------------------------------
@router.get("/forecast", response_model=S.ForecastResponse)
async def forecast(entity_id: list[str] | None = Query(default=None)) -> S.ForecastResponse:
    engine = get_engine()
    if entity_id:
        unknown = [e for e in entity_id if e not in engine.store.nodes]
        if unknown:
            raise ApiError("ENTITY_NOT_FOUND", f"No entity with id '{unknown[0]}'.", {"entity_id": unknown[0]})
    payload = engine.forecast_payload(entity_id)
    if not payload["forecasts"] and not entity_id:
        raise ApiError("MODEL_NOT_READY", "The forecaster is still warming up.")
    return S.ForecastResponse(**payload)


@router.get("/forecast/pressure-timeline", response_model=S.PressureTimelineResponse)
async def pressure_timeline() -> S.PressureTimelineResponse:
    store = get_engine().store
    return S.PressureTimelineResponse(sim_time=store.sim_time, items=store.pressure_timeline)


# --- §3.5 cascade --------------------------------------------------------------
@router.get("/cascade/active", response_model=S.ActiveCascadesResponse)
async def active_cascades() -> S.ActiveCascadesResponse:
    cached = CACHE.get("cascade:active")
    payload = cached or get_engine().cascade_payload()
    return S.ActiveCascadesResponse(**payload)


@router.get("/cascade/{entity_id}", response_model=S.CascadeResult)
async def cascade(entity_id: str) -> S.CascadeResult:
    engine = get_engine()
    store = engine.store
    if entity_id not in store.nodes:
        raise ApiError("ENTITY_NOT_FOUND", f"No entity with id '{entity_id}'.", {"entity_id": entity_id})

    existing = store.cascades.get(entity_id)
    if existing:
        return S.CascadeResult(**existing)

    # Not currently a cascade root: compute on demand so the click-through works.
    result = await asyncio.to_thread(
        engine.registry.cascade.predict,
        entity_id,
        store.node_state_for_ml(),
        store.edges,
        None,
        store.sim_time,
    )
    return S.CascadeResult(**result)


# --- §3.6 interventions ---------------------------------------------------------
@router.get("/interventions", response_model=S.InterventionListResponse)
async def interventions(
    status: str = Query(default="proposed"), limit: int = Query(default=10, ge=1, le=100)
) -> S.InterventionListResponse:
    store = get_engine().store
    items = store.interventions_by_status(None if status == "all" else status, limit)
    return S.InterventionListResponse(
        sim_time=store.sim_time, interventions=[S.Intervention(**_clean(i)) for i in items]
    )


@router.get("/interventions/{intervention_id}", response_model=S.Intervention)
async def intervention(intervention_id: str) -> S.Intervention:
    store = get_engine().store
    item = store.interventions.get(intervention_id)
    if not item:
        raise ApiError(
            "INTERVENTION_NOT_FOUND", f"No intervention with id '{intervention_id}'.",
            {"intervention_id": intervention_id},
        )
    return S.Intervention(**_clean(item))


@router.post("/interventions/{intervention_id}/approve", response_model=S.ApproveResponse)
async def approve(intervention_id: str, body: S.ApproveRequest) -> S.ApproveResponse:
    engine = get_engine()
    store = engine.store
    item = store.interventions.get(intervention_id)
    if not item:
        raise ApiError(
            "INTERVENTION_NOT_FOUND", f"No intervention with id '{intervention_id}'.",
            {"intervention_id": intervention_id},
        )
    if item["status"] != "proposed":
        raise ApiError(
            "INTERVENTION_ALREADY_RESOLVED",
            f"Intervention '{intervention_id}' is already {item['status']}.",
            {"intervention_id": intervention_id, "status": item["status"]},
        )
    if parse(item["expires_at"]) <= parse(store.sim_time):
        raise ApiError(
            "INTERVENTION_EXPIRED", f"Intervention '{intervention_id}' expired at {item['expires_at']}.",
            {"intervention_id": intervention_id},
        )

    # Claimed synchronously (no await in between), so a double-click or a
    # second operator cannot apply the same action twice.
    item["status"] = "approved"
    try:
        applied = await asyncio.to_thread(engine.approve_intervention, item)
    except Exception:
        item["status"] = "proposed"
        raise
    nudges = issue_nudges(engine, item)
    _audit(engine, f"operator:{body.operator_id}", "approve", intervention_id,
           {"note": body.note, "compliance": applied["compliance"]})
    _record_execution(engine, item, body.operator_id, True, body.note, applied["branch_id"])

    await MANAGER.broadcast(
        "intervention_resolved", {"intervention_id": intervention_id, "status": "executing"}, store.sim_time
    )
    for nudge in nudges:
        await MANAGER.broadcast("nudge_pushed", {"nudge": public_nudge(nudge)}, store.sim_time)

    return S.ApproveResponse(
        intervention_id=intervention_id,
        status="executing",
        applied_at=store.sim_time,
        nudges_issued=len(nudges),
        twin_branch_id=applied["branch_id"],
    )


@router.post("/interventions/{intervention_id}/reject", response_model=S.RejectResponse)
async def reject(intervention_id: str, body: S.RejectRequest) -> S.RejectResponse:
    engine = get_engine()
    store = engine.store
    item = store.interventions.get(intervention_id)
    if not item:
        raise ApiError(
            "INTERVENTION_NOT_FOUND", f"No intervention with id '{intervention_id}'.",
            {"intervention_id": intervention_id},
        )
    if item["status"] != "proposed":
        raise ApiError(
            "INTERVENTION_ALREADY_RESOLVED",
            f"Intervention '{intervention_id}' is already {item['status']}.",
            {"intervention_id": intervention_id, "status": item["status"]},
        )

    item["status"] = "rejected"
    item["_rejected_at"] = store.sim_time
    store.root_cooldown[item.get("triggered_by_entity_id") or ""] = store.cycle_number
    _audit(engine, f"operator:{body.operator_id}", "reject", intervention_id, {"reason": body.reason})
    _record_execution(engine, item, body.operator_id, False, body.reason, None)
    await MANAGER.broadcast(
        "intervention_resolved", {"intervention_id": intervention_id, "status": "rejected"}, store.sim_time
    )
    return S.RejectResponse(intervention_id=intervention_id, status="rejected")


# --- §3.7 certificates ------------------------------------------------------------
@router.get("/certificates/{intervention_id}", response_model=S.Certificate)
async def certificate(intervention_id: str) -> S.Certificate:
    cert = get_engine().store.certificates.get(intervention_id)
    if not cert:
        raise ApiError(
            "INTERVENTION_NOT_FOUND", f"No certificate for '{intervention_id}'.",
            {"intervention_id": intervention_id},
        )
    return S.Certificate(**cert)


# --- §3.8 simulation -----------------------------------------------------------------
@router.post("/simulate", response_model=S.SimulateAcceptedResponse, status_code=202)
async def simulate(body: S.SimulateRequest, background: BackgroundTasks) -> S.SimulateAcceptedResponse:
    if not body.scenarios:
        raise ApiError("INVALID_SCENARIO", "At least one scenario is required.")
    if not (60 <= body.horizon_sec <= 7200):
        raise ApiError("INVALID_HORIZON", "horizon_sec must be between 60 and 7200.", {"horizon_sec": body.horizon_sec})

    engine = get_engine()
    for sc in body.scenarios:
        _validate_scenario(engine, sc.scenario_type, sc.params)
    simulation_id = SIMULATIONS.new_id()
    SIMULATIONS.create(simulation_id, body.label)
    scenarios = [s.model_dump() for s in body.scenarios]
    background.add_task(SIMULATIONS.run, engine, simulation_id, scenarios, body.horizon_sec)
    return S.SimulateAcceptedResponse(simulation_id=simulation_id, status="running",
                                      eta_sec=max(1, int(body.horizon_sec // 1800)))


@router.get("/simulate/{simulation_id}", response_model=S.SimulationResult)
async def simulation(simulation_id: str) -> S.SimulationResult:
    job = SIMULATIONS.get(simulation_id)
    if not job:
        raise ApiError(
            "SIMULATION_NOT_FOUND", f"No simulation with id '{simulation_id}'.",
            {"simulation_id": simulation_id},
        )
    payload = dict(job)
    payload["candidate_interventions"] = [_clean(i) for i in payload.get("candidate_interventions", [])]
    return S.SimulationResult(**payload)


# --- §3.9 twin --------------------------------------------------------------------------
@router.get("/twin/fidelity", response_model=S.TwinFidelity)
async def twin_fidelity() -> S.TwinFidelity:
    payload = get_engine().store.twin_fidelity_payload()
    if not payload:
        raise ApiError("MODEL_NOT_READY", "The twin has not completed its first assimilation yet.")
    return S.TwinFidelity(**payload)


@router.post("/twin/drift-mode", response_model=S.DriftModeResponse)
async def drift_mode(body: S.DriftModeRequest) -> S.DriftModeResponse:
    result = get_engine().set_drift_mode(body.enabled)
    return S.DriftModeResponse(**result)


# --- §3.10 regret / metrics ----------------------------------------------------------------
@router.get("/regret", response_model=S.RegretResponse)
async def regret() -> S.RegretResponse:
    return S.RegretResponse(**build_regret(get_engine()))


@router.get("/metrics", response_model=S.MetricsResponse)
async def metrics() -> S.MetricsResponse:
    return S.MetricsResponse(**build_metrics(get_engine()))


# --- §3.11 commander -------------------------------------------------------------------------
@router.post("/commander/query", response_model=S.CommanderResponse)
async def commander(body: S.CommanderQueryRequest) -> S.CommanderResponse:
    if not body.query.strip():
        raise ApiError("INVALID_REQUEST", "Query must not be empty.")
    result = await get_engine().commander.answer(body.query, body.session_id)
    return S.CommanderResponse(**result)


# --- §3.12 attendee -----------------------------------------------------------------------------
@router.post("/attendee/journey", response_model=S.JourneyResponse)
async def journey(body: S.JourneyRequest) -> S.JourneyResponse:
    engine = get_engine()
    result = await asyncio.to_thread(build_journey, engine, body.model_dump())
    await MANAGER.broadcast(
        "journey_risk_update",
        {
            "attendee_id": body.attendee_id,
            "journey_risk_score": result["journey_risk_score"],
            "journey_risk_band": result["journey_risk_band"],
        },
        engine.store.sim_time,
    )
    return S.JourneyResponse(**result)


@router.get("/attendee/nudges", response_model=S.NudgeListResponse)
async def nudges(attendee_id: str = Query(...)) -> S.NudgeListResponse:
    store = get_engine().store
    items = [n for n in store.nudges.values() if n["attendee_id"] == attendee_id]
    items.sort(key=lambda n: n["issued_at"], reverse=True)
    return S.NudgeListResponse(nudges=[S.Nudge(**public_nudge(n)) for n in items])


@router.post("/attendee/nudges/{nudge_id}/respond", response_model=S.NudgeRespondResponse)
async def respond(nudge_id: str, body: S.NudgeRespondRequest) -> S.NudgeRespondResponse:
    engine = get_engine()
    store = engine.store
    nudge = store.nudges.get(nudge_id)
    if not nudge:
        raise ApiError("NUDGE_NOT_FOUND", f"No nudge with id '{nudge_id}'.", {"nudge_id": nudge_id})
    if nudge["status"] != "pending":
        raise ApiError(
            "INTERVENTION_ALREADY_RESOLVED", f"Nudge '{nudge_id}' is already {nudge['status']}.",
            {"nudge_id": nudge_id},
        )

    if parse(nudge["expires_at"]) <= parse(store.sim_time):
        nudge["status"] = "expired"
        raise ApiError("INTERVENTION_EXPIRED", f"Nudge '{nudge_id}' expired at {nudge['expires_at']}.",
                       {"nudge_id": nudge_id})
    nudge["status"] = "accepted" if body.accepted else "declined"
    # The answer changes this attendee's plan and the compliance every active
    # intervention runs at in the simulator (engine.record_compliance).
    await asyncio.to_thread(apply_nudge_response, engine, nudge, bool(body.accepted))
    _audit(engine, "attendee", "nudge_response", nudge_id,
           {"accepted": body.accepted, "compliance": engine.current_compliance()})
    return S.NudgeRespondResponse(
        nudge_id=nudge_id, status=nudge["status"], compliance_recorded=True
    )


# --- §3.13 demo control -------------------------------------------------------------------------
@router.post("/demo/control", response_model=S.DemoControlResponse)
async def demo_control(body: S.DemoControlRequest) -> S.DemoControlResponse:
    engine = get_engine()
    result = await engine.demo_control(
        body.action,
        body.seed,
        body.speed_multiplier,
        body.seek_to_sim_time,
        body.inject.model_dump() if body.inject else None,
    )
    _audit(engine, "operator:demo", "demo_control", None, body.model_dump(exclude_none=True))
    if body.inject:
        await MANAGER.broadcast("disruption_update", {"disruptions": list(engine.store.disruptions.values())},
                                engine.store.sim_time)
    if body.action in ("reset", "seek"):
        # The clock moved backwards: every client must replace its state
        # wholesale (01 §4 resync) or it keeps showing the previous run.
        from .ws_routes import _resync_payload

        await MANAGER.broadcast("resync", _resync_payload(engine), engine.store.sim_time)
    return S.DemoControlResponse(**result, cycle_sec=engine.sim_dt)


# --- live city overview -----------------------------------------------------------------------
DOMAINS = {
    "venues": ("venue", "gate"),
    "transport": ("transport_node", "transport_route"),
    "roads": ("road",),
    "crowd": ("zone",),
    "parking": ("parking",),
    "hospitality": ("hotel",),
    "emergency": ("emergency_facility",),
}


@router.get("/overview", response_model=S.OverviewResponse)
async def overview() -> S.OverviewResponse:
    engine = get_engine()
    store = engine.store
    with engine.world_lock:
        closed = engine.generator.closed_entities() if hasattr(engine.generator, "closed_entities") else set()
        delays = {e: engine.generator.queue_delay_sec(e) for e in store.nodes} if hasattr(engine.generator, "queue_delay_sec") else {}
    domains: dict[str, list] = {k: [] for k in DOMAINS}
    for eid, node in store.nodes.items():
        st = store.entity_states.get(eid)
        if not st:
            continue
        fc = store.forecasts.get(eid) or {}
        f1800 = next((p["predicted_utilisation"] for p in fc.get("points", []) if p["horizon_sec"] == 1800), None)
        row = S.DomainEntity(
            entity_id=eid, display_name=node["display_name"], entity_type=node["entity_type"],
            nominal_capacity=float(node["nominal_capacity"]), current_count=st["current_count"],
            utilisation=st["utilisation"], risk_score=st["risk_score"], risk_band=st["risk_band"],
            is_observed=st["is_observed"], closed=eid in closed, forecast_1800=f1800,
            time_to_critical_sec=fc.get("time_to_critical_sec"), queue_delay_sec=int(round(delays.get(eid, 0.0))),
        )
        for domain, types in DOMAINS.items():
            if node["entity_type"] in types:
                domains[domain].append(row)
    for rows in domains.values():
        rows.sort(key=lambda r: (-r.risk_score, r.entity_id))
    return S.OverviewResponse(
        sim_time=store.sim_time, cycle_number=store.cycle_number,
        speed_multiplier=engine.speed_multiplier, paused=engine.paused,
        summary=S.StateSummary(**store.summary), operations=engine.operations_summary(), domains=domains,
    )


# --- events (schedule) -----------------------------------------------------------------------
@router.get("/events", response_model=S.EventListResponse)
async def events() -> S.EventListResponse:
    engine = get_engine()
    return S.EventListResponse(
        sim_time=engine.store.sim_time, primary_event_id=engine.events.primary_event_id,
        events=[S.EventView(**v) for v in engine.event_views()],
    )


@router.get("/events/{event_id}", response_model=S.EventView)
async def event_detail(event_id: str) -> S.EventView:
    return S.EventView(**get_engine().event_view(event_id))


@router.post("/events/{event_id}", response_model=S.EventView)
async def update_event(event_id: str, body: S.EventUpdateRequest) -> S.EventView:
    engine = get_engine()
    if body.delay_sec is None and body.start_time is None and body.expected_attendance is None and body.status is None:
        raise ApiError("INVALID_REQUEST", "Nothing to change: give delay_sec, start_time, expected_attendance or status.")
    view = await asyncio.to_thread(
        engine.update_event, event_id,
        delay_sec=body.delay_sec, start_time=body.start_time,
        expected_attendance=body.expected_attendance, status=body.status,
    )
    _audit(engine, f"operator:{body.operator_id}", "event_update", event_id,
           body.model_dump(exclude_none=True, exclude={"operator_id"}))
    _persist_event(engine, event_id)
    await MANAGER.broadcast("event_updated", {"event": view}, engine.store.sim_time)
    return S.EventView(**view)


# --- accommodation ------------------------------------------------------------------------------
@router.get("/accommodation/hotels", response_model=S.HotelListResponse)
async def hotels(
    venue_entity_id: str | None = Query(default=None),
    zone: str | None = Query(default=None),
    tier: str | None = Query(default=None),
    max_price_paise: int | None = Query(default=None, ge=0),
    min_rooms: int = Query(default=0, ge=0),
    accessible_only: bool = Query(default=False),
    status: str | None = Query(default=None),
    sort: str = Query(default="occupancy"),
) -> S.HotelListResponse:
    result = await asyncio.to_thread(
        ACC.hotel_list, get_engine(), venue_entity_id, zone, tier, max_price_paise, min_rooms,
        accessible_only, status, sort,
    )
    return S.HotelListResponse(**result)


@router.get("/accommodation/hotels/{property_id}", response_model=S.HotelProperty)
async def hotel(property_id: str, venue_entity_id: str | None = Query(default=None)) -> S.HotelProperty:
    return S.HotelProperty(**ACC.get_property(get_engine(), property_id, venue_entity_id))


@router.post("/accommodation/recommend", response_model=S.StayRecommendationResponse)
async def recommend_stay(body: S.StayRecommendationRequest) -> S.StayRecommendationResponse:
    engine = get_engine()
    dest = body.destination_entity_id
    if body.event_id:
        dest = engine.events.get(body.event_id)["venue_entity_id"]
    result = await asyncio.to_thread(
        ACC.recommend, engine, dest, body.segment_id, body.max_price_paise, body.accessible_only,
        body.rooms, body.current_property_id, body.limit,
    )
    return S.StayRecommendationResponse(**result)


@router.get("/accommodation/saturation", response_model=S.SaturationResponse)
async def hotel_saturation(venue_entity_id: str | None = Query(default=None)) -> S.SaturationResponse:
    return S.SaturationResponse(**await asyncio.to_thread(ACC.saturation, get_engine(), venue_entity_id))


# --- live disruptions ----------------------------------------------------------------------------
@router.get("/disruptions", response_model=S.DisruptionListResponse)
async def disruptions() -> S.DisruptionListResponse:
    store = get_engine().store
    return S.DisruptionListResponse(
        sim_time=store.sim_time, disruptions=[S.Disruption(**d) for d in store.disruptions.values()]
    )


@router.post("/disruptions", response_model=S.Disruption, status_code=201)
async def create_disruption(body: S.DisruptionCreateRequest) -> S.Disruption:
    engine = get_engine()
    _validate_scenario(engine, body.scenario_type, body.params)
    record = await asyncio.to_thread(engine.inject_disruption, body.scenario_type, body.params, body.label)
    _audit(engine, f"operator:{body.operator_id}", "disruption_start", record["disruption_id"],
           {"scenario_type": body.scenario_type, "params": body.params})
    await MANAGER.broadcast("disruption_update", {"disruptions": list(engine.store.disruptions.values())},
                            engine.store.sim_time)
    return S.Disruption(**record)


@router.delete("/disruptions/{disruption_id}", response_model=S.Disruption)
async def clear_disruption(disruption_id: str) -> S.Disruption:
    engine = get_engine()
    record = await asyncio.to_thread(engine.clear_disruption, disruption_id)
    _audit(engine, "operator:demo", "disruption_clear", disruption_id, {})
    await MANAGER.broadcast("disruption_update", {"disruptions": list(engine.store.disruptions.values())},
                            engine.store.sim_time)
    return S.Disruption(**record)


ENTITY_SCENARIOS = {"metro_capacity_delta", "road_capacity_delta", "gate_closure", "transport_outage", "parking_loss"}


def _validate_scenario(engine, scenario_type: str, params: dict) -> None:
    """Reject scenarios that would silently do nothing (unknown entity or event)."""
    if scenario_type == "combined":
        subs = params.get("scenarios") or []
        if not subs:
            raise ApiError("INVALID_SCENARIO", "A combined scenario needs at least one sub-scenario.")
        for sub in subs:
            _validate_scenario(engine, sub.get("scenario_type", ""), sub.get("params", {}))
        return
    if scenario_type in ENTITY_SCENARIOS:
        eid = params.get("entity_id")
        if eid not in engine.store.nodes:
            raise ApiError("INVALID_SCENARIO", f"Scenario '{scenario_type}' needs a valid entity_id.",
                           {"entity_id": eid})
    if scenario_type == "event_delay":
        engine.events.get(params.get("event_id", engine.events.primary_event_id))
    if scenario_type == "attendance_delta" and params.get("event_id"):
        engine.events.get(params["event_id"])


# --- helpers -----------------------------------------------------------------------------------------
def _clean(intervention: dict) -> dict:
    """Strip the internal bookkeeping keys before the model validates."""
    return {k: v for k, v in intervention.items() if not k.startswith("_")}


def _record_execution(engine, item: dict, operator_id: str, approved: bool, note: str | None,
                      branch_id: str | None) -> None:
    from ..db import models
    from ..db.base import SessionLocal

    try:
        with SessionLocal() as session:
            session.merge(models.Intervention(
                intervention_id=item["intervention_id"], intervention_type=item["intervention_type"],
                status=item["status"], target_entity_ids=item["target_entity_ids"],
                triggered_by_entity_id=item.get("triggered_by_entity_id"), title=item["title"],
                description=item["description"], estimated_relief_pct=item["estimated_relief_pct"],
                estimated_cost_paise=item["estimated_cost_paise"], estimated_delay_sec=item["estimated_delay_sec"],
                feasibility=item["feasibility"], rank_score=item["rank_score"],
                created_at=parse(item["created_at"]), expires_at=parse(item["expires_at"]),
            ))
            session.flush()
            session.merge(models.Execution(
                execution_id=f"exe_{item['intervention_id']}", intervention_id=item["intervention_id"],
                operator_id=operator_id, approved=approved, note=note, twin_branch_id=branch_id,
                applied_at=parse(engine.store.sim_time),
            ))
            session.commit()
    except Exception:
        log.exception("execution record failed for %s", item["intervention_id"])


def _persist_event(engine, event_id: str) -> None:
    from ..db import models
    from ..db.base import SessionLocal

    ev = engine.events.get(event_id)
    try:
        with SessionLocal() as session:
            session.merge(models.EventSchedule(
                event_id=ev["event_id"], name=ev["name"], category=ev["category"],
                venue_entity_id=ev["venue_entity_id"], start_time=parse(ev["start_time"]),
                end_time=parse(ev["end_time"]), original_start_time=parse(ev["original_start_time"]),
                expected_attendance=ev["expected_attendance"], status=ev["status"],
                updated_at=datetime.now(timezone.utc),
            ))
            session.commit()
    except Exception:
        log.exception("event persistence failed for %s", event_id)


def _audit(engine, actor: str, action: str, subject_id: str | None, detail: dict) -> None:
    from ..db import models
    from ..db.base import SessionLocal

    try:
        with SessionLocal() as session:
            session.add(
                models.AuditLog(
                    server_time=datetime.now(timezone.utc),
                    sim_time=parse(engine.store.sim_time),
                    actor=actor,
                    action=action,
                    subject_id=subject_id,
                    detail=detail,
                )
            )
            session.commit()
    except Exception:
        log.exception("audit write failed for %s/%s", actor, action)
