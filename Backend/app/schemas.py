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
    "connects_to",   # additive: a station / hotel / car park's physical connector onto the road graph
]
Confidence = Literal["high", "medium", "low"]
RiskBand = Literal["low", "moderate", "high", "critical"]
RiskType = Literal[
    "crowd", "capacity", "transport", "traffic", "hospitality",
    "parking", "emergency", "cascading", "overall",
]
CertificateVerdict = Literal["STABLE", "CONDITIONAL", "UNSTABLE"]
InterventionType = Literal[
    "reroute_transport", "deploy_shuttle", "stagger_entry", "gate_redistribution",
    "parking_redistribution", "zone_incentive", "accommodation_rebalance",
    "emergency_corridor", "notify_only", "event_delay", "transport_redistribution",
]
InterventionStatus = Literal[
    "proposed", "approved", "rejected", "executing", "completed", "expired",
]
SegmentId = Literal[
    "price_sensitive", "time_sensitive", "accessibility_constrained", "group", "premium",
]
ForecastSource = Literal["persistence", "tsfm", "local_model", "twin_model"]
CascadeSource = Literal["deterministic", "gnn"]
ScenarioType = Literal[
    "attendance_delta", "metro_capacity_delta", "road_capacity_delta",
    "weather_rain", "gate_closure", "transport_outage", "parking_loss",
    "hotel_shortage", "concurrent_event", "combined", "event_delay",
    "event_cancellation", "road_closure", "station_closure", "capacity_reduction",
]
NudgeStatus = Literal["pending", "accepted", "declined", "expired"]


class Base(BaseModel):
    """Strict base: an unexpected key from an ML module is a contract violation."""

    model_config = {"extra": "forbid"}


# --- 00 §2.1 / §2.2 ------------------------------------------------------
class Provenance(Base):
    """Where an entity/edge came from (additive). `source`: osm | organizer | derived | synthetic_demo."""
    source: str
    source_id: Optional[str] = None
    confidence: Confidence
    generated: bool = False
    inferred: bool = False
    travel_time_confidence: Optional[Confidence] = None
    lanes_source: Optional[str] = None


class Entity(Base):
    entity_id: str
    entity_type: EntityType
    display_name: str
    lat: float
    lon: float
    nominal_capacity: float
    parent_id: Optional[str] = None
    meta: dict[str, Any] = Field(default_factory=dict)
    # --- additive: semantic subtype and capacity provenance ---
    subtype: Optional[str] = None
    capacity_source: Optional[str] = None
    capacity_confidence: Optional[Confidence] = None
    provenance: Optional[Provenance] = None


class GraphEdge(Base):
    edge_id: str
    src_entity_id: str
    dst_entity_id: str
    edge_type: EdgeType
    transfer_coefficient: float
    travel_time_sec: int
    substitutability: float = 0.0
    # --- additive: generated-world geometry, derivation and provenance ---
    distance_m: Optional[float] = None
    capacity_per_min: Optional[float] = None
    directionality: Optional[Literal["oneway", "bidirectional"]] = None
    travel_time_source: Optional[str] = None
    geometry: Optional[list[list[float]]] = None          # [[lon, lat], ...] road centreline
    via_entity_ids: Optional[list[str]] = None            # road path an access -> gate route follows
    provenance: Optional[Provenance] = None


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


class Footprint(Base):
    center_lat: float
    center_lon: float
    radius_m: float
    bounds: Bounds
    area_km2: float
    ring: list[list[float]]                     # closed [lon, lat] ring


class WorldInfo(Base):
    """Which world the engine is simulating (additive)."""
    world_id: str
    source: Literal["synthetic_demo", "file", "generated_blueprint"]
    data_source: str                            # live_osm | osm_snapshot | synthetic | file
    blueprint_id: Optional[str] = None
    graph_hash: Optional[str] = None
    run_id: int
    venue_entity_id: Optional[str] = None
    venue_name: Optional[str] = None
    footprint: Optional[Footprint] = None
    venue_geometry: Optional[list[list[float]]] = None   # [lon, lat] ring when OSM maps the venue
    cascade_source: CascadeSource
    cascade_note: Optional[str] = None
    attribution: Optional[str] = None
    activated_at: str
    node_count: int
    edge_count: int


class GraphResponse(Base):
    nodes: list[Entity]
    edges: list[GraphEdge]
    segments: list[Segment]
    bounds: Bounds
    world: Optional[WorldInfo] = None


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
    # Simulation flows (people/min) and people waiting outside the entity.
    # null = not modelled for this entity type (e.g. hotels).
    inflow_per_min: Optional[float] = None
    outflow_per_min: Optional[float] = None
    queue_people: Optional[float] = None


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


class TwinCounterfactual(Base):
    intervention_id: str
    utilisation: float


class TwinView(Base):
    """The digital twin's layers for one entity: what the sensor said, what the
    twin estimates (and how sure it is), what the announced plan implies, what
    it forecasts, and what the do-nothing worlds of executing actions show."""
    observed_utilisation: Optional[float] = None
    estimated_utilisation: Optional[float] = None
    estimate_std: Optional[float] = None
    plan_utilisation: Optional[float] = None
    forecast_1800: Optional[float] = None
    over_capacity: bool = False
    over_capacity_pct: float = 0.0
    counterfactuals: list[TwinCounterfactual] = []


class EntityDetailResponse(Base):
    state: EntityState
    forecast: Optional[Forecast] = None
    edges_in: list[GraphEdge]
    edges_out: list[GraphEdge]
    risk_breakdown: list[RiskBreakdownItem]
    twin: Optional[TwinView] = None


# --- 00 §2.5 -------------------------------------------------------------
class CascadeStep(Base):
    step_index: int
    entity_id: str
    predicted_band: RiskBand
    eta_sec: int
    failure_probability: float
    via_edge_id: Optional[str] = None
    depth: int
    # Why this step exists (deterministic flow cascade): which entity passes
    # load along `via_edge_id`, how many people, and the load before/after.
    source_entity_id: Optional[str] = None
    utilisation_before: Optional[float] = None
    utilisation_after: Optional[float] = None
    flow_change_people: Optional[float] = None
    reason: Optional[str] = None
    confidence: Optional[float] = None  # ML failure probability, when a model produced one


class CascadeResult(Base):
    root_entity_id: str
    source: CascadeSource
    generated_at: str
    total_downstream_failures: int
    max_depth: int
    steps: list[CascadeStep]
    ml_enhanced: bool = False


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
class InterventionEvaluation(Base):
    """What simulating the candidate on a clone of the live city showed."""

    horizon_sec: int
    compliance: float
    root_entity_id: Optional[str] = None
    root_peak_before: float
    root_peak_after: float
    network_peak_before: float
    network_peak_after: float
    critical_before: int
    critical_after: int
    new_critical_entities: list[str] = Field(default_factory=list)
    people_redirected: int = 0
    travel_time_delta_sec: float = 0.0
    rooms_unmet_delta: float = 0.0


class InterventionEffect(Base):
    """Live consequence of an executing action: the live city vs its do-nothing copy."""
    entity_id: str
    utilisation: float
    counterfactual_utilisation: float
    delta: float


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
    evaluation: Optional[InterventionEvaluation] = None
    live_effect: Optional[list[InterventionEffect]] = None


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
    plan_change: dict[str, Any] = Field(default_factory=dict)


# --- 01 §3.1 health ------------------------------------------------------
class ModuleHealth(Base):
    ready: bool
    active_source: Optional[str] = None
    ensemble_size: Optional[int] = None
    detail: Optional[str] = None


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
    avg_utilisation: Optional[float] = None
    transport_pressure: Optional[float] = None
    road_pressure: Optional[float] = None
    venue_pressure: Optional[float] = None
    hotel_pressure: Optional[float] = None
    queued_people: Optional[float] = None
    late_entries: Optional[float] = None
    rooms_unmet: Optional[float] = None
    unmet_demand: Optional[float] = None
    avg_travel_time_sec: Optional[float] = None


class SimulationDelta(Base):
    peak_utilisation_pct: float
    load_variance_pct: float
    new_critical_entities: list[str]
    resolved_critical_entities: list[str] = Field(default_factory=list)
    metrics: dict[str, float] = Field(default_factory=dict)


class SimulationChange(Base):
    entity_id: str
    display_name: str
    entity_type: EntityType
    baseline_peak: float
    scenario_peak: float


class SimulationTimelinePoint(Base):
    offset_sec: int
    baseline_peak: float
    scenario_peak: float


class SimulationResult(Base):
    simulation_id: str
    status: Literal["running", "complete", "failed"]
    label: Optional[str] = None
    baseline: Optional[SimulationSide] = None
    scenario: Optional[SimulationSide] = None
    delta: Optional[SimulationDelta] = None
    cascade: Optional[CascadeResult] = None
    candidate_interventions: list[Intervention] = Field(default_factory=list)
    top_changes: list[SimulationChange] = Field(default_factory=list)
    timeline: list[SimulationTimelinePoint] = Field(default_factory=list)
    horizon_sec: Optional[int] = None
    scenarios: list[ScenarioSpec] = Field(default_factory=list)


# --- 01 §3.10 metrics ----------------------------------------------------
class MetricValue(Base):
    value: float
    baseline: Optional[float] = None
    baseline_name: Optional[str] = None
    improvement_pct: Optional[float] = None
    target: Optional[float] = None
    target_range: Optional[list[float]] = None
    sample_size: Optional[int] = None


class MetricsResponse(Base):
    sim_time: str
    prediction: dict[str, MetricValue]
    twin: dict[str, MetricValue]
    decision: dict[str, MetricValue]
    system: dict[str, MetricValue]
    operations: dict[str, MetricValue] = Field(default_factory=dict)


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
RoutePriority = Literal["fastest", "balanced", "least_crowded"]


class GeoTravelResponse(Base):
    from_entity_id: str
    to_entity_id: str
    mode: str
    provider: str
    source: str
    duration_sec: int
    distance_m: int


class JourneyRequest(Base):
    attendee_id: str
    segment_id: SegmentId
    origin_entity_id: str
    destination_entity_id: str
    planned_departure: Optional[str] = None
    priority: RoutePriority = "balanced"
    include_return: bool = True
    hotel_property_id: Optional[str] = None
    transport_preference: Optional[Literal["any", "metro", "bus", "car", "walk"]] = None


class RouteLeg(Base):
    from_entity_id: str
    to_entity_id: str
    mode: Literal["walk", "transit", "shuttle", "drive"]
    duration_sec: int


class Route(Base):
    legs: list[RouteLeg]
    total_duration_sec: int
    predicted_crowding_band: RiskBand
    label: Optional[str] = None
    peak_utilisation: Optional[float] = None
    congestion_delay_sec: int = 0
    depart_at: Optional[str] = None


class DepartureOption(Base):
    offset_sec: int
    depart_at: str
    arrive_at: str
    travel_time_sec: int
    peak_utilisation: float
    crowding_band: RiskBand
    meets_event_start: Optional[bool] = None
    recommended: bool = False


class JourneyResponse(Base):
    journey_risk_score: int
    journey_risk_band: RiskBand
    recommended_route: Route
    shortest_route: Route
    advice: str
    priority: Optional[RoutePriority] = None
    alternatives: list[Route] = Field(default_factory=list)
    departure_options: list[DepartureOption] = Field(default_factory=list)
    recommended_departure_offset_sec: Optional[int] = None
    departure_advice: Optional[str] = None
    return_route: Optional[Route] = None
    event: Optional[dict[str, Any]] = None
    avoided_entity_ids: list[str] = Field(default_factory=list)
    transport_preference: Optional[str] = None
    preference_met: Optional[bool] = None


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
    cycle_sec: Optional[int] = None


# --- events (schedule) --------------------------------------------------
EventStatus = Literal["scheduled", "arriving", "live", "egress", "ended", "cancelled"]


class EventView(Base):
    event_id: str
    name: str
    category: str
    venue_entity_id: str
    venue_name: str
    start_time: str
    end_time: str
    original_start_time: str
    original_end_time: str
    delay_sec: int
    expected_attendance: int
    out_of_town_share: float
    status: EventStatus
    arrived: int
    inside: int
    description: Optional[str] = None
    # Exact windows the simulator uses (explicit, or the configured default curve).
    arrival_window_start: Optional[str] = None
    arrival_window_end: Optional[str] = None
    departure_window_start: Optional[str] = None
    departure_window_end: Optional[str] = None
    custom_windows: bool = False
    remaining_demand: int = 0     # visitors still to come (0 once cancelled)
    # --- accommodation demand (additive) ---
    lodging_share: Optional[float] = None             # the event's own value; null = configured default
    lodging_share_effective: Optional[float] = None   # what the simulation uses
    lodging_share_source: Optional[Literal["event", "default"]] = None
    lodging_guests: Optional[int] = None              # attendance x lodging share (people needing a room)
    local_guests: Optional[int] = None                # everyone else (travel from home)
    lodging_allocated_guests: Optional[int] = None    # lodging guests who got a room so far (simulated)
    lodging_unmet_guests: Optional[int] = None        # lodging guests no hotel in the network could take


class EventListResponse(Base):
    sim_time: str
    primary_event_id: str
    events: list[EventView]


class EventUpdateRequest(Base):
    operator_id: str
    delay_sec: Optional[int] = Field(default=None, ge=-7200, le=14400)
    start_time: Optional[str] = None
    expected_attendance: Optional[int] = Field(default=None, ge=0, le=500000)
    status: Optional[Literal["scheduled", "cancelled"]] = None
    note: Optional[str] = None
    end_time: Optional[str] = None
    name: Optional[str] = Field(default=None, max_length=120)
    venue_entity_id: Optional[str] = None
    category: Optional[str] = Field(default=None, max_length=40)
    description: Optional[str] = Field(default=None, max_length=500)
    lodging_share: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    arrival_window_start: Optional[str] = None     # "" clears an explicit window
    arrival_window_end: Optional[str] = None
    departure_window_start: Optional[str] = None
    departure_window_end: Optional[str] = None


class EventCreateRequest(Base):
    operator_id: str
    name: str = Field(min_length=1, max_length=120)
    venue_entity_id: str
    start_time: str                 # exact datetime; no zone = UTC (the simulation clock)
    end_time: str
    expected_attendance: int = Field(ge=0, le=500000)
    category: str = Field(default="event", max_length=40)
    description: Optional[str] = Field(default=None, max_length=500)
    out_of_town_share: float = Field(default=0.25, ge=0.0, le=1.0)
    lodging_share: Optional[float] = Field(default=None, ge=0.0, le=1.0)   # null = configured default
    status: Literal["scheduled", "cancelled"] = "scheduled"
    arrival_window_start: Optional[str] = None
    arrival_window_end: Optional[str] = None
    departure_window_start: Optional[str] = None
    departure_window_end: Optional[str] = None


class EventVenue(Base):
    entity_id: str
    display_name: str
    entity_type: EntityType
    nominal_capacity: float


class EventVenueList(Base):
    venues: list[EventVenue]


class EventDeleteResponse(Base):
    event_id: str
    name: str
    status: Literal["deleted"]
    arrived: int     # visitors the event already brought; they leave normally
    inside: int


# --- accommodation ------------------------------------------------------
PropertyStatus = Literal["available", "limited", "saturated"]
Tier = Literal["budget", "midscale", "upscale", "luxury"]


class HotelProperty(Base):
    property_id: str
    name: str
    cluster_entity_id: str
    zone: str
    lat: float
    lon: float
    tier: Optional[Tier] = None                 # null = unknown (generated hotels without stars)
    accessible: bool
    price_per_night_paise: Optional[int] = None  # null = unknown: generated hotels have no sourced price
    rooms_total: int
    rooms_source: Optional[str] = None
    rooms_confidence: Optional[Confidence] = None
    price_source: Optional[str] = None
    rooms_in_service: int
    rooms_occupied: int
    rooms_available: int
    occupancy: float
    status: PropertyStatus
    transport_entity_id: Optional[str] = None
    transport_name: Optional[str] = None
    walk_to_transport_sec: int
    transport_utilisation: float
    transport_closed: bool = False
    transport_forecast_utilisation: Optional[float] = None
    venue_entity_id: str
    travel_time_to_venue_sec: Optional[int] = None
    # --- capacity + occupancy breakdown (additive) ---
    # rooms_in_service = bookable rooms (bed-limited, after shortages) = rooms_occupied + rooms_available.
    bed_capacity: Optional[int] = None               # mapped beds (or capacity:persons); null = not mapped
    bed_source: Optional[str] = None
    effective_guest_capacity: Optional[float] = None
    guest_capacity_source: Optional[str] = None
    baseline_rooms_occupied: Optional[float] = None  # non-event guests (simulated baseline, not observed)
    event_rooms_occupied: Optional[float] = None     # rooms held by event guests now (simulated)
    event_guests: Optional[float] = None
    occupancy_baseline_source: Optional[str] = None
    capacity_tags: Optional[dict[str, str]] = None   # the raw OSM capacity tags the value came from


class LodgingEvent(Base):
    """One event's accommodation demand (people). lodging = allocated + unmet + pending."""
    event_id: str
    name: str
    attendance: float
    lodging_share: float
    lodging_share_source: Literal["event", "default"]
    lodging_guests: float
    local_guests: float
    requested_guests: float
    allocated_guests: float
    unmet_guests: float
    pending_guests: float
    in_house_guests: float
    checked_out_guests: float
    hotel_origin_share: float
    cancelled: bool


class HotelSummary(Base):
    properties: int
    rooms_in_service: int
    rooms_occupied: int
    rooms_available: int
    occupancy: float
    saturated: int
    limited: int
    unmet_room_requests: int
    # --- accommodation demand aggregates (additive; people unless named rooms) ---
    effective_guest_capacity: Optional[float] = None
    attendance_total: Optional[float] = None
    local_guests: Optional[float] = None
    lodging_guests: Optional[float] = None
    lodging_requested_guests: Optional[float] = None
    lodging_allocated_guests: Optional[float] = None
    lodging_unmet_guests: Optional[float] = None
    lodging_pending_guests: Optional[float] = None
    lodging_in_house_guests: Optional[float] = None
    lodging_checked_out_guests: Optional[float] = None
    default_lodging_share: Optional[float] = None
    guests_per_room: Optional[float] = None
    shortage_message: Optional[str] = None


class HotelListResponse(Base):
    sim_time: str
    summary: HotelSummary
    hotels: list[HotelProperty]
    lodging_by_event: list[LodgingEvent] = Field(default_factory=list)


class StayRecommendationRequest(Base):
    destination_entity_id: Optional[str] = None
    event_id: Optional[str] = None
    segment_id: Optional[SegmentId] = None
    max_price_paise: Optional[int] = Field(default=None, ge=0)
    accessible_only: bool = False
    rooms: int = Field(default=1, ge=1, le=50)
    current_property_id: Optional[str] = None
    limit: int = Field(default=5, ge=1, le=20)


class StayOption(Base):
    property: HotelProperty
    score: float
    factors: dict[str, float]
    travel_time_sec: Optional[int] = None
    reasons: list[str]


class StayRecommendationResponse(Base):
    sim_time: str
    destination_entity_id: str
    options: list[StayOption]
    explanation: str
    current: Optional[HotelProperty] = None
    no_availability_reason: Optional[str] = None


class SaturatedProperty(Base):
    property: HotelProperty
    alternatives: list[StayOption]
    explanation: str


class SaturationResponse(Base):
    sim_time: str
    saturated: list[SaturatedProperty]


# --- live disruptions -----------------------------------------------------
class DisruptionCreateRequest(Base):
    scenario_type: ScenarioType
    params: dict[str, Any] = Field(default_factory=dict)
    label: Optional[str] = None
    operator_id: str = "op_demo"


class Disruption(Base):
    disruption_id: str
    scenario_type: ScenarioType
    params: dict[str, Any]
    label: Optional[str] = None
    started_at: str


class DisruptionListResponse(Base):
    sim_time: str
    disruptions: list[Disruption]


# --- operations overview (live city by domain) ------------------------------
class DomainEntity(Base):
    entity_id: str
    display_name: str
    entity_type: EntityType
    nominal_capacity: float
    current_count: float
    utilisation: float
    risk_score: int
    risk_band: RiskBand
    is_observed: bool
    closed: bool = False
    forecast_1800: Optional[float] = None
    time_to_critical_sec: Optional[int] = None
    queue_delay_sec: int = 0
    inflow_per_min: Optional[float] = None
    outflow_per_min: Optional[float] = None
    queue_people: Optional[float] = None       # waiting outside; never part of current_count
    available_capacity: Optional[float] = None
    event_ids: list[str] = Field(default_factory=list)


class OverviewResponse(Base):
    sim_time: str
    cycle_number: int
    speed_multiplier: float
    paused: bool
    summary: StateSummary
    operations: dict[str, Any]
    domains: dict[str, list[DomainEntity]]


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


# --- venue → radius → footprint → blueprint → event graph (additive) ----------------
class Venue(Base):
    venue_id: str
    display_name: str
    formatted_address: Optional[str] = None
    lat: float
    lon: float
    source: Literal["coordinates", "nominatim", "google_places"]
    source_id: str
    confidence: Confidence
    category: Optional[str] = None
    osm_type: Optional[str] = None
    osm_id: Optional[int] = None
    bbox: Optional[list[float]] = None
    google_place_id: Optional[str] = None
    persistable: bool = True
    osm_capacity: Optional[int] = None


class ProviderAttempt(Base):
    provider: str
    status: Literal["ok", "failed", "not_configured"]
    reason: Optional[str] = None
    results: Optional[int] = None


class VenueSearchResponse(Base):
    query: str
    results: list[Venue]
    provider: Optional[str] = None
    attempts: list[ProviderAttempt]
    all_failed: bool
    attribution: list[str]


class VenueSelection(Base):
    """A search result (venue_id), an OSM reference, or organiser coordinates."""
    venue_id: Optional[str] = None
    source: Optional[Literal["coordinates", "nominatim", "google_places"]] = None
    osm_type: Optional[Literal["node", "way", "relation"]] = None
    osm_id: Optional[int] = Field(default=None, ge=1)
    lat: Optional[float] = None
    lon: Optional[float] = None
    display_name: Optional[str] = Field(default=None, max_length=120)


class BlueprintBuildRequest(Base):
    venue: VenueSelection
    radius_m: float
    venue_capacity: Optional[int] = Field(default=None, ge=100, le=500000)
    max_gates: Optional[int] = Field(default=None, ge=1, le=16)


class BlueprintSummary(Base):
    road_nodes: int
    road_edges: int
    transport_nodes: int
    hotels: int
    parking: int
    emergency: int
    access_points: int
    zones: int
    venues: int
    total_nodes: int
    total_edges: int
    transit_by_subtype: dict[str, int]
    gate_inference: str


class ValidationReport(Base):
    valid: bool
    errors: list[str]
    warnings: list[str]
    checks: dict[str, bool]
    counts: dict[str, int]


class BlueprintMetadata(Base):
    data_source: str
    provider: Optional[str] = None
    endpoint: Optional[str] = None
    osm_base_timestamp: Optional[str] = None
    attribution: str
    query_sha1: Optional[str] = None
    fetch_ms: Optional[float] = None
    parameters: dict[str, Any]
    timings_ms: dict[str, float] = Field(default_factory=dict)


class BlueprintHeader(Base):
    blueprint_id: str
    graph_hash: str
    venue: Venue
    venue_entity_id: str
    radius_m: float
    footprint: Footprint
    summary: BlueprintSummary
    warnings: list[str]
    provenance_summary: dict[str, dict[str, int]]
    metadata: BlueprintMetadata
    validation: ValidationReport
    persistable: bool
    generated_at: str
    active: bool = False


class BlueprintListResponse(Base):
    blueprints: list[BlueprintHeader]


class HotelPropertyRecord(Base):
    property_id: str
    name: str
    cluster_entity_id: str
    zone: str
    lat: float
    lon: float
    rooms_total: int
    price_per_night_paise: Optional[int] = None
    tier: Optional[Tier] = None
    accessible: bool
    transport_entity_id: Optional[str] = None
    walk_to_transport_sec: int
    base_occupancy: float
    rooms_source: Optional[str] = None
    rooms_confidence: Optional[Confidence] = None
    price_source: Optional[str] = None
    tier_source: Optional[str] = None
    provenance: Optional[Provenance] = None
    bed_capacity: Optional[int] = None
    bed_source: Optional[str] = None
    effective_guest_capacity: Optional[float] = None
    guest_capacity_source: Optional[str] = None
    capacity_tags: Optional[dict[str, str]] = None
    occupancy_baseline_source: Optional[str] = None


class Blueprint(BlueprintHeader):
    venue_geometry: Optional[list[list[float]]] = None
    nodes: list[Entity]
    edges: list[GraphEdge]
    zones: list[str]
    properties: list[HotelPropertyRecord]
    bounds: Bounds
    source_summary: dict[str, dict[str, int]]


class BlueprintJob(Base):
    build_id: str
    status: Literal["running", "complete", "failed"]
    stage: str
    stages: list[dict[str, Any]]
    error: Optional[dict[str, Any]] = None
    blueprint_id: Optional[str] = None
    header: Optional[BlueprintHeader] = None


class ActivationEvent(Base):
    name: Optional[str] = Field(default=None, max_length=120)
    expected_attendance: Optional[int] = Field(default=None, ge=0, le=500000)
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    category: str = Field(default="event", max_length=40)
    out_of_town_share: float = Field(default=0.25, ge=0.0, le=1.0)
    lodging_share: Optional[float] = Field(default=None, ge=0.0, le=1.0)


class BlueprintActivateRequest(Base):
    operator_id: str = "operator"
    event: Optional[ActivationEvent] = None


class GeospatialStatus(Base):
    venue_providers: list[dict[str, Any]]
    geo_provider: str
    geo_live: bool
    overpass_endpoints: list[str]
    radius_min_m: float
    radius_max_m: float
    radius_default_m: float
    attribution: list[str]
    world: WorldInfo
