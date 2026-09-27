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
    def __init__(self, events: list[dict[str, Any]], primary_event_id: str, demand: dict | None = None) -> None:
        self.demand = dict(demand or {})
        self._config_events = [dict(e) for e in events]
        self.primary_event_id = primary_event_id
        self.events: dict[str, dict[str, Any]] = {}
        self.reset()

    def reset(self) -> None:
        self.events = {}
        self.deleted: dict[str, dict[str, Any]] = {}
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
                "lodging_share": self._lodging(e.get("lodging_share")),
                "status": "scheduled",
                "description": e.get("description"),
                "arrival_window_start": None, "arrival_window_end": None,
                "departure_window_start": None, "departure_window_end": None,
            }
            self.events[ev["event_id"]] = ev

    @staticmethod
    def _lodging(value: Any) -> float | None:
        """Share of attendees who need accommodation: None = use the configured default;
        otherwise 0.0-1.0, rejected (never clamped) outside that range."""
        if value is None:
            return None
        try:
            v = float(value)
        except (TypeError, ValueError):
            raise ApiError("INVALID_REQUEST", "lodging_share must be a number between 0 and 1.", {"lodging_share": value})
        if not (0.0 <= v <= 1.0) or v != v:
            raise ApiError("INVALID_REQUEST", "lodging_share must be between 0 and 1.", {"lodging_share": value})
        return v

    # --- queries -----------------------------------------------------------------
    def get(self, event_id: str) -> dict[str, Any]:
        ev = self.events.get(event_id)
        if not ev:
            raise ApiError("EVENT_NOT_FOUND", f"No event with id '{event_id}'.", {"event_id": event_id})
        return ev

    def to_generator(self) -> list[dict[str, Any]]:
        """Active schedule plus deleted events (which only drain their visitors)."""
        return [copy.deepcopy(e) for e in list(self.events.values()) + list(self.deleted.values())]

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
            "lodging_share": ev.get("lodging_share"),
            # effective accommodation demand of this event (simulation state)
            "lodging_share_effective": live.get("lodging_share"),
            "lodging_share_source": live.get("lodging_share_source"),
            "lodging_guests": _int_or_none(live.get("lodging_guests")),
            "local_guests": _int_or_none(live.get("local_guests")),
            "lodging_allocated_guests": _int_or_none(live.get("allocated_guests")),
            "lodging_unmet_guests": _int_or_none(live.get("unmet_guests")),
            "venue_name": venue_name or ev["venue_entity_id"],
            "status": self.phase(ev, sim_time),
            "delay_sec": delay,
            "arrived": int(round(live.get("arrived", 0.0))),
            "inside": int(round(live.get("inside", 0.0))),
            "description": ev.get("description"),
            # Visitors still to come: none once cancelled.
            "remaining_demand": 0 if ev["status"] == "cancelled"
            else max(0, int(ev["expected_attendance"]) - int(round(live.get("arrived", 0.0)))),
            "custom_windows": bool(ev.get("arrival_window_start") or ev.get("departure_window_start")),
            **self._effective_windows(ev),
        }

    def _effective_windows(self, ev: dict) -> dict[str, str]:
        """The arrival/departure windows the simulator actually uses (explicit,
        or the configured default curve: mid +- 2 sd)."""
        d = self.demand
        out = {}
        if ev.get("arrival_window_start"):
            out["arrival_window_start"], out["arrival_window_end"] = ev["arrival_window_start"], ev["arrival_window_end"]
        else:
            mid = shift(ev["start_time"], -60 * float(d.get("arrival_lead_min", 40)))
            sd = 60 * float(d.get("arrival_sd_min", 28))
            out["arrival_window_start"], out["arrival_window_end"] = shift(mid, -2 * sd), shift(mid, 2 * sd)
        if ev.get("departure_window_start"):
            out["departure_window_start"], out["departure_window_end"] = ev["departure_window_start"], ev["departure_window_end"]
        else:
            mid = shift(ev["end_time"], 60 * float(d.get("egress_lag_min", 15)))
            sd = 60 * float(d.get("egress_sd_min", 10))
            out["departure_window_start"], out["departure_window_end"] = shift(mid, -2 * sd), shift(mid, 2 * sd)
        return out

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
    WINDOW_KEYS = ("arrival_window_start", "arrival_window_end", "departure_window_start", "departure_window_end")
    MAX_DURATION_SEC = 24 * 3600

    @staticmethod
    def _utc(value: str, field: str) -> str:
        """Exact datetime -> canonical 'YYYY-MM-DDTHH:MM:SSZ'. A value without a
        zone is read as UTC (the simulation clock's zone), never server-local."""
        try:
            dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except ValueError:
            raise ApiError("INVALID_SCHEDULE", f"{field} is not a valid date-time.", {field: value})
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return iso(dt)

    def _check_schedule(self, ev: dict[str, Any]) -> None:
        start, end = parse(ev["start_time"]), parse(ev["end_time"])
        if end <= start:
            raise ApiError("INVALID_SCHEDULE", "The event must end after it starts.",
                           {"start_time": ev["start_time"], "end_time": ev["end_time"]})
        if (end - start).total_seconds() > self.MAX_DURATION_SEC:
            raise ApiError("INVALID_SCHEDULE", "An event cannot last longer than 24 hours.", {"event_id": ev.get("event_id")})
        a0, a1 = ev.get("arrival_window_start"), ev.get("arrival_window_end")
        d0, d1 = ev.get("departure_window_start"), ev.get("departure_window_end")
        if bool(a0) != bool(a1) or bool(d0) != bool(d1):
            raise ApiError("INVALID_SCHEDULE", "A window needs both a start and an end.", {"event_id": ev.get("event_id")})
        if a0 and (parse(a1) <= parse(a0) or parse(a1) > end):
            raise ApiError("INVALID_SCHEDULE", "The arrival window must be a real interval that closes by the end of the event.",
                           {"arrival_window_start": a0, "arrival_window_end": a1})
        if d0 and (parse(d1) <= parse(d0) or parse(d0) < start):
            raise ApiError("INVALID_SCHEDULE", "The departure window must be a real interval that opens after the start.",
                           {"departure_window_start": d0, "departure_window_end": d1})

    def _new_id(self, name: str) -> str:
        slug = "".join(c if c.isalnum() else "_" for c in name.lower())
        slug = "_".join(p for p in slug.split("_") if p)[:40] or "event"
        base = "evt_" + slug
        eid, n = base, 2
        while eid in self.events or eid in self.deleted:
            eid, n = f"{base}_{n}", n + 1
        return eid

    def create(self, sim_time: str, *, name: str, venue_entity_id: str, start_time: str, end_time: str,
               expected_attendance: int, category: str = "event", description: str | None = None,
               out_of_town_share: float = 0.25, status: str = "scheduled", lodging_share: float | None = None,
               **windows: str | None) -> dict[str, Any]:
        name = (name or "").strip()
        if not name:
            raise ApiError("INVALID_REQUEST", "An event needs a name.")
        ev = {
            "event_id": self._new_id(name), "name": name, "category": (category or "event").strip().lower() or "event",
            "description": description, "venue_entity_id": venue_entity_id,
            "start_time": self._utc(start_time, "start_time"), "end_time": self._utc(end_time, "end_time"),
            "expected_attendance": int(expected_attendance), "out_of_town_share": float(out_of_town_share),
            "lodging_share": self._lodging(lodging_share),
            "status": status if status in ("scheduled", "cancelled") else "scheduled",
        }
        ev["original_start_time"], ev["original_end_time"] = ev["start_time"], ev["end_time"]
        for k in self.WINDOW_KEYS:
            ev[k] = self._utc(windows[k], k) if windows.get(k) else None
        if ev["expected_attendance"] < 0:
            raise ApiError("INVALID_REQUEST", "expected_attendance must be >= 0.")
        self._check_schedule(ev)
        if parse(ev["end_time"]) <= parse(sim_time):
            raise ApiError("INVALID_SCHEDULE", "The event would already be over at the current simulation time.",
                           {"end_time": ev["end_time"], "sim_time": sim_time})
        self.events[ev["event_id"]] = ev
        return ev

    def delete(self, event_id: str) -> dict[str, Any]:
        """Remove from the active schedule. The simulator keeps it as 'deleted' so
        visitors already in the city leave normally; it generates no new demand."""
        ev = self.events.pop(self.get(event_id)["event_id"])
        ev["status"] = "deleted"
        self.deleted[event_id] = ev
        return ev

    def update(
        self,
        event_id: str,
        sim_time: str,
        delay_sec: int | None = None,
        start_time: str | None = None,
        expected_attendance: int | None = None,
        status: str | None = None,
        end_time: str | None = None,
        name: str | None = None,
        venue_entity_id: str | None = None,
        category: str | None = None,
        description: str | None = None,
        lodging_share: float | None = None,
        **windows: str | None,
    ) -> dict[str, Any]:
        ev = self.get(event_id)
        if ev["status"] == "cancelled" and status != "scheduled":
            raise ApiError("EVENT_CANCELLED", f"Event '{event_id}' is cancelled; restore it before editing.",
                           {"event_id": event_id})
        now = parse(sim_time)
        started = parse(ev["start_time"]) <= now
        new = dict(ev)
        duration = (parse(ev["end_time"]) - parse(ev["start_time"])).total_seconds()
        if start_time is not None:
            new_start = parse(self._utc(start_time, "start_time"))
        elif delay_sec is not None:
            new_start = datetime.fromtimestamp(parse(ev["start_time"]).timestamp() + int(delay_sec), tz=timezone.utc)
        else:
            new_start = None
        if new_start is not None and new_start != parse(ev["start_time"]):
            if started:
                raise ApiError(
                    "INVALID_SCHEDULE",
                    "The event has already started; its start cannot move (the end, attendance and status can).",
                    {"event_id": event_id},
                )
            if new_start <= now:
                raise ApiError(
                    "INVALID_SCHEDULE", "An event that has not started cannot be moved into the past.",
                    {"event_id": event_id, "start_time": iso(new_start)},
                )
            new["start_time"] = iso(new_start)
            offset = (new_start - parse(ev["start_time"])).total_seconds()
            if end_time is None:
                new["end_time"] = shift(new["start_time"], duration)
            for k in self.WINDOW_KEYS:   # explicit windows move with the event
                if ev.get(k) and windows.get(k) is None:
                    new[k] = shift(ev[k], offset)
        if end_time is not None:
            new["end_time"] = self._utc(end_time, "end_time")
            if parse(new["end_time"]) <= now < parse(ev["end_time"]):
                raise ApiError("INVALID_SCHEDULE", "The end cannot be moved into the past.", {"end_time": new["end_time"]})
        for k in self.WINDOW_KEYS:
            if windows.get(k) is not None:
                new[k] = self._utc(windows[k], k) if windows[k] else None   # "" clears a window
        if name is not None:
            if not name.strip():
                raise ApiError("INVALID_REQUEST", "An event needs a name.")
            new["name"] = name.strip()
        if venue_entity_id is not None:
            new["venue_entity_id"] = venue_entity_id
        if category is not None:
            new["category"] = category.strip().lower() or "event"
        if description is not None:
            new["description"] = description
        if lodging_share is not None:
            new["lodging_share"] = self._lodging(lodging_share)
        if expected_attendance is not None:
            if expected_attendance < 0:
                raise ApiError("INVALID_REQUEST", "expected_attendance must be >= 0.", {"event_id": event_id})
            new["expected_attendance"] = int(expected_attendance)
        if status is not None:
            if status not in ("scheduled", "cancelled"):
                raise ApiError("INVALID_REQUEST", "status must be 'scheduled' or 'cancelled'.", {"status": status})
            new["status"] = status
        self._check_schedule(new)
        ev.clear()
        ev.update(new)
        return ev


def _int_or_none(v: Any) -> int | None:
    return None if v is None else int(round(float(v)))
