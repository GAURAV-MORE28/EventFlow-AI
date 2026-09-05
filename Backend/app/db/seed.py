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
