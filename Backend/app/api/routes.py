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
from ..services.attendee import build_journey, issue_nudges
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
    cfg = get_config().raw["event"]
    return S.EventResponse(
        event_id=cfg["event_id"],
        name=cfg["name"],
        venue_entity_id=cfg["venue_entity_id"],
        expected_attendance=int(cfg["expected_attendance"]),
        start_time=cfg["start_time"],
        end_time=cfg["end_time"],
        sim_time=engine.store.sim_time,
        concurrent_events=[],
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

    # A counterfactual branch is forked at approval time so the regret ledger can
    # later answer "what if we had done nothing" with evidence rather than a guess.
    branch = await asyncio.to_thread(engine.registry.twin.branch, {}, 1800)
    item["status"] = "executing"
    item["_applied_at"] = store.sim_time
    item["_counterfactual_relief_pct"] = 0.0
    item["_twin_branch_id"] = branch["branch_id"]

    nudges = issue_nudges(engine, item)
    _audit(engine, f"operator:{body.operator_id}", "approve", intervention_id, {"note": body.note})

    await MANAGER.broadcast(
        "intervention_resolved", {"intervention_id": intervention_id, "status": "executing"}, store.sim_time
    )
    for nudge in nudges:
        await MANAGER.broadcast("nudge_pushed", {"nudge": nudge}, store.sim_time)

    return S.ApproveResponse(
        intervention_id=intervention_id,
        status="executing",
        applied_at=store.sim_time,
        nudges_issued=len(nudges),
        twin_branch_id=branch["branch_id"],
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
    _audit(engine, f"operator:{body.operator_id}", "reject", intervention_id, {"reason": body.reason})
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
    simulation_id = SIMULATIONS.new_id()
    SIMULATIONS.create(simulation_id, body.label)
    scenarios = [s.model_dump() for s in body.scenarios]
    background.add_task(SIMULATIONS.run, engine, simulation_id, scenarios, body.horizon_sec)
    return S.SimulateAcceptedResponse(simulation_id=simulation_id, status="running", eta_sec=3)


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
    result = build_journey(engine, body.model_dump())
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
    return S.NudgeListResponse(nudges=[S.Nudge(**n) for n in items])


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

    nudge["status"] = "accepted" if body.accepted else "declined"
    # Observed compliance feeds back into the solver's elasticities within the run.
    store.observed_compliance.append(bool(body.accepted))
    _audit(engine, "attendee", "nudge_response", nudge_id, {"accepted": body.accepted})
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
    return S.DemoControlResponse(**result)


# --- helpers -----------------------------------------------------------------------------------------
def _clean(intervention: dict) -> dict:
    """Strip the internal bookkeeping keys before the model validates."""
    return {k: v for k, v in intervention.items() if not k.startswith("_")}


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
