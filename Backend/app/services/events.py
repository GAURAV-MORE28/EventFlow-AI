"""Event schedule — the demand source for the whole city model.

Events come from `config.yaml` (`events:`) at startup and on reset; operators
can then delay, reschedule, resize or cancel them. Every change is pushed into
the simulator (live world, the twin's nominal model and any running
counterfactuals), so a delay reshapes arrivals, transport, roads and venue
pressure — it is not a label change.
"""
from __future__ import annotations

import copy
import logging
from datetime import datetime, timezone
from typing import Any

from ..errors import ApiError
from ..simtime import iso, parse, shift

log = logging.getLogger("eventflow.events")

ARRIVAL_WINDOW_SEC = 2 * 3600   # "arriving" status starts this long before an event


class EventSchedule:
    def __init__(self, events: list[dict[str, Any]], primary_event_id: str) -> None:
        self._config_events = [dict(e) for e in events]
        self.primary_event_id = primary_event_id
        self.events: dict[str, dict[str, Any]] = {}
        self.reset()

    def reset(self) -> None:
        self.events = {}
        for e in self._config_events:
            ev = {
                "event_id": e["event_id"],
                "name": e["name"],
                "category": e.get("category", "event"),
                "venue_entity_id": e["venue_entity_id"],
                "start_time": e["start_time"],
                "end_time": e["end_time"],
                "original_start_time": e["start_time"],
                "original_end_time": e["end_time"],
                "expected_attendance": int(e["expected_attendance"]),
                "out_of_town_share": float(e.get("out_of_town_share", 0.25)),
                "status": "scheduled",
            }
            self.events[ev["event_id"]] = ev

    # --- queries -----------------------------------------------------------------
    def get(self, event_id: str) -> dict[str, Any]:
        ev = self.events.get(event_id)
        if not ev:
            raise ApiError("EVENT_NOT_FOUND", f"No event with id '{event_id}'.", {"event_id": event_id})
        return ev

    def to_generator(self) -> list[dict[str, Any]]:
        return [copy.deepcopy(e) for e in self.events.values()]

    def phase(self, ev: dict, sim_time: str) -> str:
        if ev["status"] == "cancelled":
            return "cancelled"
        now, start, end = parse(sim_time), parse(ev["start_time"]), parse(ev["end_time"])
        if now >= end:
            return "egress" if (now - end).total_seconds() < 3600 else "ended"
        if now >= start:
            return "live"
        if (start - now).total_seconds() <= ARRIVAL_WINDOW_SEC:
            return "arriving"
        return "scheduled"

    def view(self, ev: dict, sim_time: str, live: dict | None, venue_name: str | None) -> dict[str, Any]:
        delay = int((parse(ev["start_time"]) - parse(ev["original_start_time"])).total_seconds())
        live = live or {}
        return {
            **{k: ev[k] for k in (
                "event_id", "name", "category", "venue_entity_id", "start_time", "end_time",
                "original_start_time", "original_end_time", "expected_attendance", "out_of_town_share",
            )},
            "venue_name": venue_name or ev["venue_entity_id"],
            "status": self.phase(ev, sim_time),
            "delay_sec": delay,
            "arrived": int(round(live.get("arrived", 0.0))),
            "inside": int(round(live.get("inside", 0.0))),
        }

    def concurrent_with(self, event_id: str) -> list[dict[str, Any]]:
        """Events whose [start-2h, end+1h] window overlaps the given event's."""
        me = self.get(event_id)
        a0 = parse(me["start_time"]).timestamp() - ARRIVAL_WINDOW_SEC
        a1 = parse(me["end_time"]).timestamp() + 3600
        out = []
        for ev in self.events.values():
            if ev["event_id"] == event_id or ev["status"] == "cancelled":
                continue
            b0 = parse(ev["start_time"]).timestamp() - ARRIVAL_WINDOW_SEC
            b1 = parse(ev["end_time"]).timestamp() + 3600
            if a0 < b1 and b0 < a1:
                out.append(ev)
        return out

    # --- changes -------------------------------------------------------------------
    def update(
        self,
        event_id: str,
        sim_time: str,
        delay_sec: int | None = None,
        start_time: str | None = None,
        expected_attendance: int | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        ev = self.get(event_id)
        if ev["status"] == "cancelled" and status != "scheduled":
            raise ApiError("EVENT_CANCELLED", f"Event '{event_id}' is cancelled.", {"event_id": event_id})
        duration = (parse(ev["end_time"]) - parse(ev["start_time"])).total_seconds()
        if start_time is not None:
            new_start = parse(start_time)
        elif delay_sec is not None:
            new_start = parse(ev["start_time"]).timestamp() + int(delay_sec)
            new_start = datetime.fromtimestamp(new_start, tz=timezone.utc)
        else:
            new_start = None
        if new_start is not None:
            if new_start <= parse(sim_time) and parse(ev["start_time"]) > parse(sim_time):
                raise ApiError(
                    "INVALID_SCHEDULE", "An event that has not started cannot be moved into the past.",
                    {"event_id": event_id, "start_time": iso(new_start)},
                )
            if parse(ev["start_time"]) <= parse(sim_time) and new_start != parse(ev["start_time"]):
                raise ApiError(
                    "INVALID_SCHEDULE", "The event has already started; only its attendance or status can change.",
                    {"event_id": event_id},
                )
            ev["start_time"] = iso(new_start)
            ev["end_time"] = shift(ev["start_time"], duration)
        if expected_attendance is not None:
            if expected_attendance < 0:
                raise ApiError("INVALID_REQUEST", "expected_attendance must be >= 0.", {"event_id": event_id})
            ev["expected_attendance"] = int(expected_attendance)
        if status is not None:
            if status not in ("scheduled", "cancelled"):
                raise ApiError("INVALID_REQUEST", "status must be 'scheduled' or 'cancelled'.", {"status": status})
            ev["status"] = status
        return ev
