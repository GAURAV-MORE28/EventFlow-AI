"""Persist the static topology so the DB matches what `GET /graph` serves.

Idempotent: re-running is a no-op, so a restart never duplicates rows and never
fights whatever is already there.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select

from . import models
from .base import SessionLocal

log = logging.getLogger("eventflow.seed")


def seed_topology(store: Any) -> None:
    try:
        with SessionLocal() as session:
            existing = set(session.scalars(select(models.Entity.entity_id)).all())
            new_entities = [
                models.Entity(
                    entity_id=n["entity_id"], entity_type=n["entity_type"],
                    display_name=n["display_name"], lat=n["lat"], lon=n["lon"],
                    nominal_capacity=n["nominal_capacity"], parent_id=n["parent_id"],
                    meta=n["meta"],
                )
                for n in store.nodes.values()
                if n["entity_id"] not in existing
            ]
            session.add_all(new_entities)
            session.flush()  # entities must exist before the edges that reference them

            existing_edges = set(session.scalars(select(models.GraphEdge.edge_id)).all())
            session.add_all(
                [
                    models.GraphEdge(**e)
                    for e in store.edges
                    if e["edge_id"] not in existing_edges
                ]
            )

            existing_props = set(session.scalars(select(models.HotelProperty.property_id)).all())
            session.add_all([
                models.HotelProperty(**{k: p[k] for k in (
                    "property_id", "name", "cluster_entity_id", "zone", "lat", "lon", "rooms_total",
                    "price_per_night_paise", "tier", "accessible", "transport_entity_id",
                    "walk_to_transport_sec", "base_occupancy",
                )})
                for p in getattr(store, "properties", []) if p["property_id"] not in existing_props
            ])

            existing_segments = set(session.scalars(select(models.Segment.segment_id)).all())
            session.add_all(
                [
                    models.Segment(**s)
                    for s in store.segments
                    if s["segment_id"] not in existing_segments
                ]
            )
            session.commit()
            log.info(
                "topology seeded: %d new entities, %d edges, %d segments",
                len(new_entities), len(store.edges) - len(existing_edges), len(store.segments),
            )
    except Exception:
        # The live path reads from StateStore, not the DB, so a seeding failure
        # degrades persistence — it must not stop the demo from starting.
        log.exception("topology seeding failed; continuing with in-memory topology")


def clear_run_tables() -> None:
    """Wipe the per-run time series (never the static topology).

    `entity_state` and `risk_state` key on `(entity_id, sim_time)`; `sim_time`
    is fully determined by `sim_start + cycle_number * cycle_sec` under a fixed
    seed (01 §8: "seed 42 reproduces the identical run twice"). That means
    *any* restart of the sim clock — a fresh process pointed at an existing
    `eventflow.db`, or an in-process `POST /demo/control {action: reset}` —
    recomputes the exact same `sim_time` sequence and collides with rows a
    previous run already wrote, aborting every subsequent persist. Clearing
    the run tables whenever the clock restarts is what makes reproducing the
    run actually mean reproducing it, at the DB layer too.
    """
    try:
        with SessionLocal() as session:
            session.query(models.EntityState).delete()
            session.query(models.RiskState).delete()
            session.query(models.Forecast).delete()
            session.commit()
        log.info("cleared entity_state/risk_state/forecast for a fresh run")
    except Exception:
        log.exception("clearing run tables failed; continuing with in-memory topology")


def persist_events(events: list[dict]) -> None:
    """Write the current schedule (called at startup, on reset and on change)."""
    from datetime import datetime, timezone

    from ..simtime import parse

    try:
        with SessionLocal() as session:
            for ev in events:
                session.merge(models.EventSchedule(
                    event_id=ev["event_id"], name=ev["name"], category=ev.get("category", "event"),
                    venue_entity_id=ev["venue_entity_id"], start_time=parse(ev["start_time"]),
                    end_time=parse(ev["end_time"]),
                    original_start_time=parse(ev.get("original_start_time", ev["start_time"])),
                    expected_attendance=int(ev["expected_attendance"]), status=ev.get("status", "scheduled"),
                    updated_at=datetime.now(timezone.utc),
                ))
            session.commit()
    except Exception:
        log.exception("event schedule persistence failed")
