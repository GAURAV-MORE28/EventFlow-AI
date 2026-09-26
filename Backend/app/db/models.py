"""SQLAlchemy mappings of the authoritative DDL in 01_BACKEND_CONTRACT.md §5.

Column names and types match the DDL one-for-one. Two portability adaptations,
both noted in the contract review:
  * `geom GEOGRAPHY(POINT, 4326)` -> `lat` / `lon` doubles (what the API exposes).
  * `TEXT[]` -> a JSON array (SQLite has no array type; Postgres stores it as JSONB).
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON, BigInteger, Boolean, DateTime, Float, ForeignKey, Index, Integer, Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base

# BIGSERIAL on Postgres; SQLite only auto-increments a plain INTEGER primary key.
AutoId = BigInteger().with_variant(Integer, "sqlite")


class Entity(Base):
    __tablename__ = "entity"

    entity_id: Mapped[str] = mapped_column(Text, primary_key=True)
    entity_type: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    lat: Mapped[float] = mapped_column(Float, nullable=False)
    lon: Mapped[float] = mapped_column(Float, nullable=False)
    nominal_capacity: Mapped[float] = mapped_column(Float, nullable=False)
    parent_id: Mapped[str | None] = mapped_column(Text, ForeignKey("entity.entity_id"), nullable=True)
    meta: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)


class GraphEdge(Base):
    __tablename__ = "graph_edge"

    edge_id: Mapped[str] = mapped_column(Text, primary_key=True)
    src_entity_id: Mapped[str] = mapped_column(Text, ForeignKey("entity.entity_id"), nullable=False)
    dst_entity_id: Mapped[str] = mapped_column(Text, ForeignKey("entity.entity_id"), nullable=False)
    edge_type: Mapped[str] = mapped_column(Text, nullable=False)
    transfer_coefficient: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    travel_time_sec: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    substitutability: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)


Index("ix_graph_edge_src", GraphEdge.src_entity_id)
Index("ix_graph_edge_dst", GraphEdge.dst_entity_id)


class Segment(Base):
    __tablename__ = "segment"

    segment_id: Mapped[str] = mapped_column(Text, primary_key=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    share: Mapped[float] = mapped_column(Float, nullable=False)
    price_elasticity: Mapped[float] = mapped_column(Float, nullable=False)
    time_elasticity: Mapped[float] = mapped_column(Float, nullable=False)
    accessibility_constrained: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    compliance_base_rate: Mapped[float] = mapped_column(Float, nullable=False)


class EntityState(Base):
    __tablename__ = "entity_state"

    entity_id: Mapped[str] = mapped_column(Text, ForeignKey("entity.entity_id"), primary_key=True)
    sim_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    current_count: Mapped[float] = mapped_column(Float, nullable=False)
    utilisation: Mapped[float] = mapped_column(Float, nullable=False)
    flow_rate_per_min: Mapped[float] = mapped_column(Float, nullable=False)
    risk_score: Mapped[int] = mapped_column(Integer, nullable=False)
    risk_band: Mapped[str] = mapped_column(Text, nullable=False)
    is_observed: Mapped[bool] = mapped_column(Boolean, nullable=False)


Index("ix_entity_state_sim_time", EntityState.sim_time.desc())


class Forecast(Base):
    __tablename__ = "forecast"

    forecast_id: Mapped[int] = mapped_column(AutoId, primary_key=True, autoincrement=True)
    entity_id: Mapped[str] = mapped_column(Text, ForeignKey("entity.entity_id"), nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    horizon_sec: Mapped[int] = mapped_column(Integer, nullable=False)
    predicted_utilisation: Mapped[float] = mapped_column(Float, nullable=False)
    lower_90: Mapped[float | None] = mapped_column(Float, nullable=True)
    upper_90: Mapped[float | None] = mapped_column(Float, nullable=True)
    time_to_critical_sec: Mapped[int | None] = mapped_column(Integer, nullable=True)


Index("ix_forecast_entity_generated", Forecast.entity_id, Forecast.generated_at.desc())


class RiskState(Base):
    __tablename__ = "risk_state"

    entity_id: Mapped[str] = mapped_column(Text, ForeignKey("entity.entity_id"), primary_key=True)
    sim_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    risk_type: Mapped[str] = mapped_column(Text, primary_key=True)
    score: Mapped[int] = mapped_column(Integer, nullable=False)


class CascadePrediction(Base):
    __tablename__ = "cascade_prediction"

    cascade_id: Mapped[int] = mapped_column(AutoId, primary_key=True, autoincrement=True)
    root_entity_id: Mapped[str] = mapped_column(Text, ForeignKey("entity.entity_id"), nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    steps: Mapped[list] = mapped_column(JSON, nullable=False)
    total_downstream_failures: Mapped[int] = mapped_column(Integer, nullable=False)
    # Model whose probabilities annotate the steps (`confidence`); null when none did.
    model_version: Mapped[str | None] = mapped_column(Text, nullable=True)


class MLNodePrediction(Base):
    """The cascade model's per-entity output every cycle, in every gnn_mode that
    calls it (shadow included). Joined with `entity_state` on (entity_id,
    sim_time) this is the evidence for evaluating the model on live runs."""

    __tablename__ = "ml_node_prediction"
    __table_args__ = (Index("ix_ml_node_prediction_entity_time", "entity_id", "sim_time"),)

    prediction_id: Mapped[int] = mapped_column(AutoId, primary_key=True, autoincrement=True)
    entity_id: Mapped[str] = mapped_column(Text, ForeignKey("entity.entity_id"), nullable=False)
    sim_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    model_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    gnn_mode: Mapped[str] = mapped_column(Text, nullable=False)
    p_fail_900: Mapped[float | None] = mapped_column(Float, nullable=True)
    p_fail_1800: Mapped[float | None] = mapped_column(Float, nullable=True)
    p_fail_3600: Mapped[float | None] = mapped_column(Float, nullable=True)
    ttc_sec: Mapped[int | None] = mapped_column(Integer, nullable=True)
    calibrated: Mapped[bool] = mapped_column(Boolean, nullable=False)
    topology_match: Mapped[bool | None] = mapped_column(Boolean, nullable=True)


class Intervention(Base):
    __tablename__ = "intervention"

    intervention_id: Mapped[str] = mapped_column(Text, primary_key=True)
    intervention_type: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    target_entity_ids: Mapped[list] = mapped_column(JSON, nullable=False)
    triggered_by_entity_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    estimated_relief_pct: Mapped[float] = mapped_column(Float, nullable=False)
    estimated_cost_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    estimated_delay_sec: Mapped[int] = mapped_column(Integer, nullable=False)
    feasibility: Mapped[float] = mapped_column(Float, nullable=False)
    rank_score: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Certificate(Base):
    __tablename__ = "certificate"

    certificate_id: Mapped[str] = mapped_column(Text, primary_key=True)
    intervention_id: Mapped[str] = mapped_column(
        Text, ForeignKey("intervention.intervention_id"), nullable=False
    )
    verdict: Mapped[str] = mapped_column(Text, nullable=False)
    converged: Mapped[bool] = mapped_column(Boolean, nullable=False)
    iterations: Mapped[int] = mapped_column(Integer, nullable=False)
    post_nudge_variance: Mapped[float] = mapped_column(Float, nullable=False)
    baseline_variance: Mapped[float] = mapped_column(Float, nullable=False)
    max_zone_utilisation: Mapped[float] = mapped_column(Float, nullable=False)
    max_zone_entity_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    oscillation_risk: Mapped[bool] = mapped_column(Boolean, nullable=False)
    compliance_sensitivity: Mapped[float] = mapped_column(Float, nullable=False)
    compliance_sweep: Mapped[list] = mapped_column(JSON, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)


class Execution(Base):
    __tablename__ = "execution"

    execution_id: Mapped[str] = mapped_column(Text, primary_key=True)
    intervention_id: Mapped[str] = mapped_column(
        Text, ForeignKey("intervention.intervention_id"), nullable=False
    )
    operator_id: Mapped[str] = mapped_column(Text, nullable=False)
    approved: Mapped[bool] = mapped_column(Boolean, nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    twin_branch_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    applied_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RegretEntry(Base):
    __tablename__ = "regret_entry"

    regret_id: Mapped[str] = mapped_column(Text, primary_key=True)
    intervention_id: Mapped[str] = mapped_column(
        Text, ForeignKey("intervention.intervention_id"), nullable=False
    )
    intervention_type: Mapped[str] = mapped_column(Text, nullable=False)
    predicted_relief_pct: Mapped[float] = mapped_column(Float, nullable=False)
    realised_relief_pct: Mapped[float] = mapped_column(Float, nullable=False)
    counterfactual_relief_pct: Mapped[float] = mapped_column(Float, nullable=False)
    regret: Mapped[float] = mapped_column(Float, nullable=False)
    sim_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Nudge(Base):
    __tablename__ = "nudge"

    nudge_id: Mapped[str] = mapped_column(Text, primary_key=True)
    attendee_id: Mapped[str] = mapped_column(Text, nullable=False)
    intervention_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("intervention.intervention_id"), nullable=True
    )
    segment_id: Mapped[str | None] = mapped_column(Text, ForeignKey("segment.segment_id"), nullable=True)
    headline: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    tradeoff: Mapped[dict] = mapped_column(JSON, nullable=False)
    target_entity_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AuditLog(Base):
    __tablename__ = "audit_log"

    audit_id: Mapped[int] = mapped_column(AutoId, primary_key=True, autoincrement=True)
    server_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sim_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    subject_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    detail: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)


class CommanderLog(Base):
    __tablename__ = "commander_log"

    log_id: Mapped[int] = mapped_column(AutoId, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(Text, nullable=False)
    query: Mapped[str] = mapped_column(Text, nullable=False)
    response: Mapped[str] = mapped_column(Text, nullable=False)
    tool_calls: Mapped[list] = mapped_column(JSON, nullable=False)
    numbers_emitted: Mapped[list] = mapped_column(JSON, nullable=False)
    ungrounded_count: Mapped[int] = mapped_column(Integer, nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    server_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class EventSchedule(Base):
    """The event schedule, including operator changes (delays, cancellations)."""

    __tablename__ = "event_schedule"

    event_id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    venue_entity_id: Mapped[str] = mapped_column(Text, ForeignKey("entity.entity_id"), nullable=False)
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    original_start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expected_attendance: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class HotelProperty(Base):
    """Static accommodation catalogue (app/catalog.py); occupancy is live state."""

    __tablename__ = "hotel_property"

    property_id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    cluster_entity_id: Mapped[str] = mapped_column(Text, ForeignKey("entity.entity_id"), nullable=False)
    zone: Mapped[str] = mapped_column(Text, nullable=False)
    lat: Mapped[float] = mapped_column(Float, nullable=False)
    lon: Mapped[float] = mapped_column(Float, nullable=False)
    rooms_total: Mapped[int] = mapped_column(Integer, nullable=False)
    price_per_night_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    tier: Mapped[str] = mapped_column(Text, nullable=False)
    accessible: Mapped[bool] = mapped_column(Boolean, nullable=False)
    transport_entity_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    walk_to_transport_sec: Mapped[int] = mapped_column(Integer, nullable=False)
    base_occupancy: Mapped[float] = mapped_column(Float, nullable=False)
