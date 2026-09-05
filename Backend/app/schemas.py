"""Pydantic models — the single validation gate between ML/DB and the wire.

Every field name, unit and casing here comes verbatim from `00_SHARED_CONTRACT.md`.
Nothing leaves the process without passing through one of these models
(01_BACKEND_CONTRACT.md §1, "the backend never returns a raw ML object").
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

# --- 00 §1 enums (frozen) ------------------------------------------------
EntityType = Literal[
    "venue", "zone", "transport_node", "transport_route", "road",
    "hotel", "parking", "gate", "emergency_facility",
]
EdgeType = Literal[
    "feeds", "adjacent_to", "serves", "last_mile_to", "substitutes_for", "evacuates_to",
]
RiskBand = Literal["low", "moderate", "high", "critical"]
RiskType = Literal[
    "crowd", "capacity", "transport", "traffic", "hospitality",
    "parking", "emergency", "cascading", "overall",
]
CertificateVerdict = Literal["STABLE", "CONDITIONAL", "UNSTABLE"]
InterventionType = Literal[
    "reroute_transport", "deploy_shuttle", "stagger_entry", "gate_redistribution",
    "parking_redistribution", "zone_incentive", "accommodation_rebalance",
    "emergency_corridor", "notify_only",
]
InterventionStatus = Literal[
    "proposed", "approved", "rejected", "executing", "completed", "expired",
]
SegmentId = Literal[
    "price_sensitive", "time_sensitive", "accessibility_constrained", "group", "premium",
]
ForecastSource = Literal["persistence", "tsfm", "local_model"]
CascadeSource = Literal["deterministic", "gnn"]
ScenarioType = Literal[
    "attendance_delta", "metro_capacity_delta", "road_capacity_delta",
    "weather_rain", "gate_closure", "transport_outage", "parking_loss",
    "hotel_shortage", "concurrent_event", "combined",
]
NudgeStatus = Literal["pending", "accepted", "declined", "expired"]


class Base(BaseModel):
    """Strict base: an unexpected key from an ML module is a contract violation."""

    model_config = {"extra": "forbid"}


# --- 00 §2.1 / §2.2 ------------------------------------------------------
class Entity(Base):
    entity_id: str
    entity_type: EntityType
    display_name: str
    lat: float
    lon: float
    nominal_capacity: float
    parent_id: Optional[str] = None
    meta: dict[str, Any] = Field(default_factory=dict)


class GraphEdge(Base):
    edge_id: str
    src_entity_id: str
    dst_entity_id: str
    edge_type: EdgeType
    transfer_coefficient: float
    travel_time_sec: int
    substitutability: float = 0.0


class Segment(Base):
    segment_id: SegmentId
    display_name: str
    share: float
    price_elasticity: float
    time_elasticity: float
    accessibility_constrained: bool
    compliance_base_rate: float


class Bounds(Base):
    min_lat: float
    max_lat: float
    min_lon: float
    max_lon: float


class GraphResponse(Base):
    nodes: list[Entity]
    edges: list[GraphEdge]
    segments: list[Segment]
    bounds: Bounds


class EventResponse(Base):
    event_id: str
    name: str
    venue_entity_id: str
    expected_attendance: int
    start_time: str
    end_time: str
    sim_time: str
    concurrent_events: list[dict[str, Any]] = Field(default_factory=list)


# --- 00 §2.3 -------------------------------------------------------------
class EntityState(Base):
    entity_id: str
    sim_time: str
    current_count: float
    utilisation: float
    flow_rate_per_min: float
    risk_score: int
    risk_band: RiskBand
    is_observed: bool


class StateSummary(Base):
    overall_risk_score: int
    overall_risk_band: RiskBand
    critical_count: int
    high_count: int
    load_variance: float


class StateResponse(Base):
    sim_time: str
    cycle_number: int
    entities: list[EntityState]
    summary: StateSummary


class RiskBreakdownItem(Base):
    risk_type: RiskType
    score: int


# --- 00 §2.4 -------------------------------------------------------------
class ForecastPoint(Base):
    horizon_sec: int
    predicted_utilisation: float
    lower_90: Optional[float] = None
    upper_90: Optional[float] = None


class BaselineComparison(Base):
    persistence_mae: float
    model_mae: float
    improvement_pct: float


class Forecast(Base):
    entity_id: str
    source: ForecastSource
    generated_at: str
    baseline_value: float
    points: list[ForecastPoint]
    time_to_critical_sec: Optional[int] = None
    baseline_comparison: Optional[BaselineComparison] = None


class ForecastResponse(Base):
    generated_at: str
    active_source: ForecastSource
    forecasts: list[Forecast]


class TrajectoryPoint(Base):
    horizon_sec: int
    utilisation: float


class PressureTimelineItem(Base):
    entity_id: str
    display_name: str
    current_utilisation: float
    current_band: RiskBand
    time_to_critical_sec: Optional[int] = None
    trajectory: list[TrajectoryPoint]


class PressureTimelineResponse(Base):
    sim_time: str
    items: list[PressureTimelineItem]


class PressureTimelineFrame(Base):
    """Shape of one entry in `pressure_timeline.json` / the `forecast_update`
    WS event's data — `PressureTimelineResponse` plus `active_source`, which
    the REST response omits but the WS broadcast (engine.py `_broadcast`)
    always carries. Mock-mode replays the WS stream, so this is the schema
    that actually describes what it replays; validating mock frames against
    `PressureTimelineResponse` itself would fail on the extra key under
    `extra="forbid"` (00 §0)."""

    sim_time: str
    active_source: ForecastSource
    items: list[PressureTimelineItem]


class EntityDetailResponse(Base):
    state: EntityState
    forecast: Optional[Forecast] = None
    edges_in: list[GraphEdge]
    edges_out: list[GraphEdge]
    risk_breakdown: list[RiskBreakdownItem]


# --- 00 §2.5 -------------------------------------------------------------
class CascadeStep(Base):
    step_index: int
    entity_id: str
    predicted_band: RiskBand
    eta_sec: int
    failure_probability: float
    via_edge_id: Optional[str] = None
    depth: int


class CascadeResult(Base):
    root_entity_id: str
    source: CascadeSource
    generated_at: str
    total_downstream_failures: int
    max_depth: int
    steps: list[CascadeStep]


class ActiveCascadesResponse(Base):
    sim_time: str
    source: CascadeSource
    cascades: list[CascadeResult]


# --- 00 §2.6 -------------------------------------------------------------
class ComplianceSweepRow(Base):
    compliance_rate: float
    max_utilisation: float
    verdict: CertificateVerdict


class Certificate(Base):
    certificate_id: str
    intervention_id: str
    verdict: CertificateVerdict
    converged: bool
    iterations: int
    post_nudge_variance: float
    baseline_variance: float
    max_zone_utilisation: float
    max_zone_entity_id: Optional[str] = None
    oscillation_risk: bool
    compliance_sensitivity: float
    compliance_sweep: list[ComplianceSweepRow]
    reason: str = Field(max_length=140)


# --- 00 §2.7 -------------------------------------------------------------
class Intervention(Base):
    intervention_id: str
    intervention_type: InterventionType
    status: InterventionStatus
    target_entity_ids: list[str]
    triggered_by_entity_id: Optional[str] = None
    title: str
    description: str
    estimated_relief_pct: float
    estimated_cost_paise: int
    estimated_delay_sec: int
    feasibility: float
    rank_score: float
    certificate: Optional[Certificate] = None
    created_at: str
    expires_at: str


class InterventionListResponse(Base):
    sim_time: str
    interventions: list[Intervention]


class ApproveRequest(Base):
    operator_id: str
    note: Optional[str] = None


class RejectRequest(Base):
    operator_id: str
    reason: Optional[str] = None


class ApproveResponse(Base):
    intervention_id: str
    status: InterventionStatus
    applied_at: str
    nudges_issued: int
    twin_branch_id: str


class RejectResponse(Base):
    intervention_id: str
    status: InterventionStatus


# --- 00 §2.8 / §2.9 ------------------------------------------------------
class TwinFidelityHistoryPoint(Base):
    sim_time: str
    assimilated_rmse: float
    uncorrected_rmse: Optional[float] = None


class TwinFidelity(Base):
    sim_time: str
    assimilated_rmse: float
    uncorrected_rmse: Optional[float] = None
    improvement_pct: Optional[float] = None
    ensemble_size: int
    ensemble_spread: float
    drift_mode_enabled: bool
    history: list[TwinFidelityHistoryPoint] = Field(default_factory=list)


class DriftModeRequest(Base):
    enabled: bool


class DriftModeResponse(Base):
    drift_mode_enabled: bool
    uncorrected_ensemble_started_at: Optional[str] = None


class RegretEntry(Base):
    regret_id: str
    intervention_id: str
    intervention_type: InterventionType
    predicted_relief_pct: float
    realised_relief_pct: float
    counterfactual_relief_pct: float
    regret: float
    sim_time: str


class RegretSummary(Base):
    count: int
    mean_absolute_regret: float
    trend_slope: float


class RegretResponse(Base):
    entries: list[RegretEntry]
    summary: RegretSummary


# --- 00 §2.11 ------------------------------------------------------------
class NudgeTradeoff(Base):
    extra_travel_sec: int
    credit_paise: int
    perk: Optional[str] = None


class Nudge(Base):
    nudge_id: str
    attendee_id: str
    intervention_id: Optional[str] = None
    headline: str
    body: str
    tradeoff: NudgeTradeoff
    target_entity_id: Optional[str] = None
    status: NudgeStatus
    issued_at: str
    expires_at: str


class NudgeListResponse(Base):
    nudges: list[Nudge]


class NudgeRespondRequest(Base):
    accepted: bool


class NudgeRespondResponse(Base):
    nudge_id: str
    status: NudgeStatus
    compliance_recorded: bool


# --- 01 §3.1 health ------------------------------------------------------
class ModuleHealth(Base):
    ready: bool
    active_source: Optional[str] = None
    ensemble_size: Optional[int] = None


class HealthResponse(Base):
    status: str
    server_time: str
    sim_time: str
    cycle_number: int
    modules: dict[str, ModuleHealth]


# --- 01 §3.8 simulate ----------------------------------------------------
class ScenarioSpec(Base):
    scenario_type: ScenarioType
    params: dict[str, Any] = Field(default_factory=dict)


class SimulateRequest(Base):
    scenarios: list[ScenarioSpec]
    horizon_sec: int = 3600
    label: Optional[str] = None


class SimulateAcceptedResponse(Base):
    simulation_id: str
    status: Literal["running", "complete", "failed"]
    eta_sec: int


class SimulationSide(Base):
    peak_utilisation: float
    peak_entity_id: Optional[str] = None
    load_variance: float
    critical_count: int


class SimulationDelta(Base):
    peak_utilisation_pct: float
    load_variance_pct: float
    new_critical_entities: list[str]


class SimulationResult(Base):
    simulation_id: str
    status: Literal["running", "complete", "failed"]
    label: Optional[str] = None
    baseline: Optional[SimulationSide] = None
    scenario: Optional[SimulationSide] = None
    delta: Optional[SimulationDelta] = None
    cascade: Optional[CascadeResult] = None
    candidate_interventions: list[Intervention] = Field(default_factory=list)


# --- 01 §3.10 metrics ----------------------------------------------------
class MetricValue(Base):
    value: float
    baseline: Optional[float] = None
    baseline_name: Optional[str] = None
    improvement_pct: Optional[float] = None
    target: Optional[float] = None
    target_range: Optional[list[float]] = None


class MetricsResponse(Base):
    sim_time: str
    prediction: dict[str, MetricValue]
    twin: dict[str, MetricValue]
    decision: dict[str, MetricValue]
    system: dict[str, MetricValue]


# --- 01 §3.11 commander --------------------------------------------------
class CommanderQueryRequest(Base):
    query: str
    session_id: str = "sess_demo"


class ToolCallRecord(Base):
    tool: str
    args: dict[str, Any]
    result_digest: str


class Grounding(Base):
    numbers_emitted: list[str]
    numbers_grounded: list[str]
    ungrounded: list[str] = Field(default_factory=list)
    ungrounded_count: int
    passed: bool


class CommanderResponse(Base):
    response: str
    is_cached: bool
    tool_calls: list[ToolCallRecord]
    grounding: Grounding


# --- 01 §3.12 attendee ---------------------------------------------------
class JourneyRequest(Base):
    attendee_id: str
    segment_id: SegmentId
    origin_entity_id: str
    destination_entity_id: str
    planned_departure: Optional[str] = None


class RouteLeg(Base):
    from_entity_id: str
    to_entity_id: str
    mode: Literal["walk", "transit", "shuttle", "drive"]
    duration_sec: int


class Route(Base):
    legs: list[RouteLeg]
    total_duration_sec: int
    predicted_crowding_band: RiskBand


class JourneyResponse(Base):
    journey_risk_score: int
    journey_risk_band: RiskBand
    recommended_route: Route
    shortest_route: Route
    advice: str


# --- 01 §3.13 demo control ----------------------------------------------
class DemoControlRequest(Base):
    action: Optional[Literal["play", "pause", "reset", "seek", "set_speed"]] = None
    seed: Optional[int] = None
    speed_multiplier: Optional[float] = None
    seek_to_sim_time: Optional[str] = None
    inject: Optional[ScenarioSpec] = None


class DemoControlResponse(Base):
    status: Literal["playing", "paused"]
    sim_time: str
    seed: int
    speed_multiplier: float


# --- 00 §3 error envelope ------------------------------------------------
class ErrorBody(Base):
    code: str
    message: str
    detail: dict[str, Any] = Field(default_factory=dict)


class ErrorEnvelope(Base):
    error: ErrorBody


# --- 01 §4 websocket -----------------------------------------------------
class WsMessage(Base):
    event: str
    sim_time: str
    seq: int
    payload: dict[str, Any]
