"""SyntheticGenerator (03_ML_CONTRACT.md §8) — a flow-coupled city model.

The generator is ground truth: the demo environment, the evaluation baseline for
the twin, and the engine behind every what-if. Earlier versions drew each entity
from its own independent curve, so nothing an operator did (or a disruption
did) could propagate. This version moves visitors through the graph:

    event schedule ──► arrivals ──► origin (hotel / home) ──► access node
    (station, bus/shuttle hub, parking lot) ──► gate ──► venue ──► egress

Deterministic and explainable, one 30s step at a time:

* **Demand** comes from the event schedule. Each event's cumulative arrivals
  follow a normal CDF peaking `arrival_lead_min` before its start; egress
  follows its end. Delaying or cancelling an event reshapes this directly.
* **Hotels**: out-of-town attendees book rooms. Bookings are allocated across
  the property catalogue by a logit over price, travel time to the venue and
  tier, capped by availability; what cannot be placed is unmet demand. Guests
  then travel to the venue from their hotel's transport node, so a hotel
  rebalance genuinely moves load between stations.
* **Access and route choice**: visitors pick lines, stations and gates in
  proportion to the graph's transfer coefficients, discounted by congestion
  observed on the previous step. A closed gate or station gets zero share, so
  its demand appears at the alternatives (00 §2.2 `substitutes_for` edges).
* **Queues**: stations and gates have a service rate; when inflow exceeds it a
  queue builds (fluid queue). Gate queues past capacity spill onto adjacent
  roads, and crowding on roads raises emergency-post load via `evacuates_to`.
* **Policies**: disruptions (injected) and approved interventions are
  modifiers — diversions with a compliance rate, service boosts, staggered
  entry, closures — recomputed whenever the set changes, removable, and
  copied into clones so a what-if uses the same physics as the live city.

No RNG anywhere: per-entity constants and noise come from `stable_unit`, so a
seed reproduces the run exactly.
"""
from __future__ import annotations

import copy
import heapq
import math
from datetime import datetime, timezone
from typing import Any

from .common import clamp, stable_unit

# --- physical constants per entity type (minutes / fractions) --------------
BG_UTIL = {
    "transport_node": 0.26, "transport_route": 0.30, "road": 0.30, "zone": 0.14,
    "parking": 0.22, "emergency_facility": 0.30, "gate": 0.0, "venue": 0.0, "hotel": 0.0,
}
STATION_DWELL_MIN = 8.0       # time an event visitor spends inside a station
STATION_MU_PER_CAP = 0.042    # station service rate, people/min per unit of capacity
GATE_MU_PER_CAP = 0.058       # gate scan rate, people/min per unit of capacity
GATE_DWELL_MIN = 2.0
GATE_HOLD_FACTOR = 1.15       # queue beyond this multiple of capacity spills onto roads
ROAD_DWELL_MIN = 3.0
ROAD_PED_SHARE = 0.30         # share of a gate's approach flow that walks along the road
ZONE_DWELL_MIN = 8.0
BOARDING_DWELL_MIN = 2.0
EMERGENCY_TRIGGER_UTIL = 0.60
EMERGENCY_GAIN = 2.0
PARKING_FULL = 0.97
CANCEL_EGRESS_MIN = 20.0    # a cancelled event empties over this many minutes
ENTRY_CLOSE_EGRESS = 0.05   # entry closes once this share of an event has left
# Physical density limits: a space cannot hold more than this multiple of its
# nominal capacity. Demand beyond it waits upstream (it is still counted in the
# queues that drive delays), it does not pile up as impossible occupancy.
ROAD_MAX = 1.6
STATION_MAX = 1.8
LINE_MAX = 1.4
ZONE_MAX = 1.6
EMERGENCY_MAX = 1.6

RAIN_FACTOR = {"light": 1.05, "moderate": 1.12, "heavy": 1.22}
TIER_SCORE = {"budget": 0.0, "midscale": 0.35, "upscale": 0.7, "luxury": 1.0}

# Always instrumented: operators put sensors on stations, gates, venues and
# booking systems report hotel occupancy directly. Roads, zones, parking and
# emergency posts are partially instrumented (`observed_fraction`).
ALWAYS_OBSERVED_TYPES = {"transport_node", "transport_route", "gate", "venue", "hotel"}

DEFAULT_DEMAND = {
    "arrival_lead_min": 40, "arrival_sd_min": 28, "egress_lag_min": 15, "egress_sd_min": 10,
    "local_mode_split": {"metro": 0.50, "bus": 0.15, "shuttle": 0.10, "car": 0.25},
    "route_sensitivity": 1.5, "persons_per_vehicle": 2.4,
    "rush_peak_elapsed_min": 240, "rush_sd_min": 70, "rush_amplitude": 0.45,
    "observed_fraction": 0.75, "sensor_dropout_rate": 0.03,
}
DEFAULT_HOSPITALITY = {
    "saturation_threshold": 0.95, "limited_threshold": 0.85, "guests_per_room": 2.2,
    "room_need_share": 0.55, "prebooked_share": 0.65, "booking_window_min": 180,
}


def _phi(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


# Keys of `__dict__` that change while the model runs; everything else is
# static structure shared (not copied) between a generator and its clones.
_DYNAMIC = (
    "seed", "_elapsed_sec", "_counts", "_util_prev", "_queue", "_parked", "_held",
    "_ev_state", "_events_base", "_modifiers", "_mod_counter", "_event_rooms",
    "_requested_rooms", "_unmet_rooms", "_displaced_rooms", "_stats", "_prev_counts",
    "_observed", "_eff", "_last_flows", "_path_time", "_flow_view",
)
# What `resimulate_last_step` rolls back: everything that evolves with time.
# The schedule and the modifier list are the *new* truth and are kept.
_RESTORABLE = tuple(k for k in _DYNAMIC if k not in ("seed", "_events_base", "_modifiers", "_mod_counter", "_eff"))


class SyntheticGenerator:
    def __init__(self, config: dict, topology: dict, seed: int = 42) -> None:
        self.config = config or {}
        self.seed = seed
        self.demand = {**DEFAULT_DEMAND, **(self.config.get("demand") or {})}
        self.hosp = {**DEFAULT_HOSPITALITY, **(self.config.get("hospitality") or {})}
        self.critical = float(self.config.get("critical_utilisation", 0.90))
        self.sim_start = self.config.get("sim_start_time", "2026-09-04T14:00:00Z")

        self.nodes: dict[str, dict[str, Any]] = {n["entity_id"]: n for n in topology["nodes"]}
        self.edges: list[dict[str, Any]] = list(topology["edges"])
        self.properties: list[dict[str, Any]] = list(topology.get("properties") or [])
        self._build_static()

        self._events_base: list[dict[str, Any]] = [dict(e) for e in (topology.get("events") or [])]
        self._modifiers: list[dict[str, Any]] = []
        self._mod_counter = 0
        self._init_dynamic()

    # --- static structure ------------------------------------------------------
    def _build_static(self) -> None:
        nodes, edges = self.nodes, self.edges
        typ = {e: n["entity_type"] for e, n in nodes.items()}
        self.types = typ
        self.cap = {e: max(1.0, float(n["nominal_capacity"])) for e, n in nodes.items()}

        out: dict[str, list[dict]] = {}
        for e in edges:
            out.setdefault(e["src_entity_id"], []).append(e)
        self.out_edges = out

        # Metro lines -> their stations (feeds coefficient).
        self.lines: dict[str, list[tuple[str, float]]] = {}
        self.station_line: dict[str, str] = {}
        for e in edges:
            if typ.get(e["src_entity_id"]) == "transport_route" and e["edge_type"] == "feeds":
                self.lines.setdefault(e["src_entity_id"], []).append((e["dst_entity_id"], e["transfer_coefficient"]))
                self.station_line[e["dst_entity_id"]] = e["src_entity_id"]
        hubs = [e for e, t in typ.items() if t == "transport_node" and e not in self.station_line]
        self.bus_hubs = [h for h in hubs if h.startswith("bus_")]
        self.shuttle_hubs = [h for h in hubs if h not in self.bus_hubs]
        self.lots = [e for e, t in typ.items() if t == "parking"]

        self.hotel_access: dict[str, list[tuple[str, float, int]]] = {}
        for e in edges:
            if typ.get(e["src_entity_id"]) == "hotel" and e["edge_type"] == "last_mile_to":
                self.hotel_access.setdefault(e["src_entity_id"], []).append(
                    (e["dst_entity_id"], e["transfer_coefficient"], int(e["travel_time_sec"]))
                )

        self.substitutes: dict[str, list[tuple[str, float]]] = {}
        for e in edges:
            if e["edge_type"] == "substitutes_for":
                self.substitutes.setdefault(e["src_entity_id"], []).append((e["dst_entity_id"], e["substitutability"]))

        # Venue options per access node: via a gate or directly.
        self.venues = [e for e, t in typ.items() if t == "venue"] + [
            e for e, t in typ.items() if t == "zone"
        ]
        self.access_nodes = list(self.station_line) + hubs + self.lots
        self.options: dict[str, dict[str, list[tuple[str | None, float, int]]]] = {}
        gate_to_venue = {
            (e["src_entity_id"], e["dst_entity_id"]): int(e["travel_time_sec"])
            for e in edges if typ.get(e["src_entity_id"]) == "gate" and e["edge_type"] == "feeds"
        }
        for v in self.venues:
            per: dict[str, list[tuple[str | None, float, int]]] = {}
            for a in self.access_nodes:
                opts: list[tuple[str | None, float, int]] = []
                for e in out.get(a, []):
                    d = e["dst_entity_id"]
                    if d == v and e["edge_type"] in ("feeds", "serves", "last_mile_to"):
                        opts.append((None, float(e["transfer_coefficient"]), int(e["travel_time_sec"])))
                    elif typ.get(d) == "gate" and e["edge_type"] in ("feeds", "serves") and (d, v) in gate_to_venue:
                        opts.append((d, float(e["transfer_coefficient"]), int(e["travel_time_sec"]) + gate_to_venue[(d, v)]))
                if opts:
                    per[a] = opts
            if per:
                self.options[v] = per
        self.venue_gates = {
            v: sorted({g for (g, vv) in gate_to_venue if vv == v}) for v in self.venues
        }

        def adj(src_type: str | None, etype: str, dst_type: str | None) -> dict[str, list[tuple[str, float]]]:
            res: dict[str, list[tuple[str, float]]] = {}
            for e in edges:
                if e["edge_type"] != etype:
                    continue
                if src_type and typ.get(e["src_entity_id"]) != src_type:
                    continue
                if dst_type and typ.get(e["dst_entity_id"]) != dst_type:
                    continue
                res.setdefault(e["src_entity_id"], []).append((e["dst_entity_id"], float(e["transfer_coefficient"])))
            return res

        self.spill_roads = {k: v for k, v in adj(None, "adjacent_to", "road").items() if typ.get(k) in ("gate", "venue")}
        road_links = adj("road", "adjacent_to", "road")
        self.road_neighbours: dict[str, list[tuple[str, float]]] = {}
        for a, lst in road_links.items():
            for b, c in lst:
                self.road_neighbours.setdefault(a, []).append((b, c))
                self.road_neighbours.setdefault(b, []).append((a, c))
        self.access_zones = {
            k: v for k, v in adj(None, "last_mile_to", "zone").items() if typ.get(k) == "transport_node"
        }
        self.gate_zones = adj("gate", "serves", "zone")
        self.lot_gates = adj("parking", "serves", "gate")
        self.evac_in: dict[str, list[tuple[str, float]]] = {}
        for e in edges:
            if e["edge_type"] == "evacuates_to":
                self.evac_in.setdefault(e["dst_entity_id"], []).append((e["src_entity_id"], float(e["transfer_coefficient"])))

        self._props_by_id = {p["property_id"]: p for p in self.properties}
        self._travel_to_venue = self._hotel_travel_times()

    def _hotel_travel_times(self) -> dict[str, dict[str, int]]:
        """Seconds from each property to each venue: walk + graph shortest path."""
        graph: dict[str, list[tuple[str, int]]] = {}
        for e in self.edges:
            if e["edge_type"] in ("feeds", "serves", "last_mile_to", "adjacent_to"):
                graph.setdefault(e["src_entity_id"], []).append((e["dst_entity_id"], int(e["travel_time_sec"]) or 120))
        out: dict[str, dict[str, int]] = {}
        for p in self.properties:
            src = p["cluster_entity_id"]
            dist = {src: 0}
            heap = [(0, src)]
            while heap:
                d, u = heapq.heappop(heap)
                if d > dist.get(u, 1e18):
                    continue
                for v, w in graph.get(u, []):
                    nd = d + w
                    if nd < dist.get(v, 1e18):
                        dist[v] = nd
                        heapq.heappush(heap, (nd, v))
            walk = int(p.get("walk_to_transport_sec", 300))
            out[p["property_id"]] = {v: dist[v] + walk for v in self.venues if v in dist}
        return out

    # --- dynamic state -------------------------------------------------------------
    def _init_dynamic(self) -> None:
        self._pre_tick = None
        self._elapsed_sec = 0.0
        self._counts: dict[str, float] = {}
        self._prev_counts: dict[str, float] = {}
        self._util_prev: dict[str, float] = {}
        self._queue: dict[str, float] = {}
        self._parked: dict[str, float] = {}
        self._held: dict[str, float] = {}
        self._ev_state: dict[str, dict[str, Any]] = {}
        self._event_rooms: dict[str, float] = {p["property_id"]: 0.0 for p in self.properties}
        self._requested_rooms = 0.0
        self._unmet_rooms = 0.0
        self._displaced_rooms = 0.0
        self._stats = {
            "diverted_people": 0.0, "late_entries": 0.0, "unparked_people": 0.0,
            "entered": 0.0, "travel_time_weighted": 0.0, "travel_people": 0.0,
        }
        self._last_flows: dict[str, float] = {}
        self._path_time: dict[str, float] = {}
        self._flow_view: dict[str, dict[str, float | None]] = {}
        frac = float(self.demand.get("observed_fraction", 0.75))
        self._observed = {
            e: (t in ALWAYS_OBSERVED_TYPES) or stable_unit(self.seed, "observed", e) < frac
            for e, t in self.types.items()
        }
        self._rebuild()
        self._allocate_rooms(initial=True)
        self._step(0.0)  # populate counts at t=0 without advancing time
        self._prev_counts = dict(self._counts)

    # --- 03 §0 universal surface ----------------------------------------------------
    def ready(self) -> bool:
        return True

    def fallback(self, dt_sec: int = 30) -> dict[str, float]:
        """Hold the last observation. A frozen world beats a blank one."""
        return {e: c for e, c in self._counts.items() if self._observed.get(e)}

    # --- events -------------------------------------------------------------------------
    def set_events(self, events: list[dict[str, Any]]) -> None:
        """Replace the event schedule (dicts in the `/events` wire shape)."""
        self._events_base = [dict(e) for e in events]
        self._allocate_rooms()

    def _minutes(self, iso: str) -> float:
        return (_parse(iso) - _parse(self.sim_start)).total_seconds() / 60.0

    def _events(self) -> list[dict[str, Any]]:
        """Effective schedule: base events with schedule/attendance modifiers applied."""
        eff = self._eff
        out = []
        for ev in self._events_base + eff["extra_events"]:
            status = ev.get("status", "scheduled")
            if ev["event_id"] in eff["cancelled_events"] or status == "deleted":
                status = "cancelled"   # a deleted event makes no new demand; its visitors leave
            if status == "cancelled":
                attendance = 0.0
            else:
                attendance = float(ev.get("expected_attendance", 0))
            attendance *= eff["attendance_mult"].get(ev["event_id"], 1.0) * eff["attendance_mult"].get("*", 1.0)
            shift = eff["event_shift_min"].get(ev["event_id"], 0.0)
            start = ev.get("start_min")
            if start is None:
                start = self._minutes(ev["start_time"])
                end = self._minutes(ev["end_time"])
            else:
                end = ev["end_min"]
            arr = self._window(ev, "arrival_window_start", "arrival_window_end", shift)
            dep = self._window(ev, "departure_window_start", "departure_window_end", shift)
            out.append({
                "event_id": ev["event_id"], "venue": ev["venue_entity_id"],
                "attendance": attendance, "start": start + shift, "end": end + shift,
                "cancelled": status == "cancelled",
                "oot": float(ev.get("out_of_town_share", 0.25)),
                # Arrival / departure curves: centre and spread in sim minutes.
                # An explicit window holds ~95% of its people (mid +- 2 sd).
                "arr_mid": arr[0] if arr else start + shift - float(self.demand["arrival_lead_min"]),
                "arr_sd": arr[1] if arr else float(self.demand["arrival_sd_min"]),
                "dep_mid": dep[0] if dep else end + shift + float(self.demand["egress_lag_min"]),
                "dep_sd": dep[1] if dep else float(self.demand["egress_sd_min"]),
            })
        return out

    def _window(self, ev: dict, k0: str, k1: str, shift: float) -> tuple[float, float] | None:
        if not ev.get(k0) or not ev.get(k1):
            return None
        a, b = self._minutes(ev[k0]) + shift, self._minutes(ev[k1]) + shift
        return ((a + b) / 2.0, max((b - a) / 4.0, 1.0))

    # --- modifiers (disruptions, interventions) -------------------------------------------
    def _new_modifier(self, source: str, kind: str, params: dict, modifier_id: str | None = None, **extra: Any) -> dict:
        self._mod_counter += 1
        mod = {
            "modifier_id": modifier_id or f"mod_{self._mod_counter:04d}",
            "source": source, "kind": kind, "params": dict(params or {}),
            "applied_at_sec": self._elapsed_sec, "until_sec": None, "compliance": None,
        }
        mod.update(extra)
        self._modifiers.append(mod)
        return mod

    def inject(self, scenario_type: str, params: dict | None = None, source: str = "disruption",
               modifier_id: str | None = None) -> str:
        """Apply a disruption/scenario. Persists until removed or reset (03 §8.3)."""
        p = dict(params or {})
        if scenario_type == "combined":
            ids = [
                self.inject(sc.get("scenario_type", ""), sc.get("params", {}), source,
                            None if modifier_id is None else f"{modifier_id}_{i}")
                for i, sc in enumerate(p.get("scenarios", []))
            ]
            return ",".join(ids)
        if scenario_type == "concurrent_event":
            main = next((e for e in self._events_base if e.get("event_id") == "evt_demo"), None)
            base_att = float(main.get("expected_attendance", 70000)) if main else 70000.0
            attendance = float(p.get("attendance") or base_att * float(p.get("overlap_pct", 15.0)) / 100.0)
            start_min = self._elapsed_sec / 60.0 + float(p.get("start_offset_min", 45))
            p.setdefault("venue_entity_id", "zone_fanpark")
            p["_event"] = {
                "event_id": p.get("event_id") or f"evt_popup_{self._mod_counter + 1:02d}",
                "name": p.get("name", "Pop-up event"),
                "venue_entity_id": p["venue_entity_id"],
                "expected_attendance": attendance,
                "start_min": start_min, "end_min": start_min + float(p.get("duration_min", 120)),
                "out_of_town_share": 0.1, "status": "scheduled",
            }
        mod = self._new_modifier(source, scenario_type, p, modifier_id=modifier_id)
        self._on_modifiers_changed(mod)
        return mod["modifier_id"]

    def remove_modifier(self, modifier_id: str) -> bool:
        """Remove a modifier (or every part of a combined one, `<id>_<n>`)."""
        before = len(self._modifiers)
        self._modifiers = [
            m for m in self._modifiers
            if m["modifier_id"] != modifier_id and not m["modifier_id"].startswith(modifier_id + "_")
        ]
        if len(self._modifiers) != before:
            self._on_modifiers_changed(None)
            return True
        return False

    def modifiers(self, source: str | None = None) -> list[dict]:
        return [dict(m) for m in self._modifiers if source is None or m["source"] == source]

    def injected(self) -> list[dict[str, Any]]:
        return [{"scenario_type": m["kind"], "params": m["params"]} for m in self._modifiers if m["source"] == "disruption"]

    def apply_intervention(self, intervention: dict, compliance: float, duration_sec: float | None = None) -> str:
        """An approved intervention's real effect on the city.

        `compliance` is the share of visitors who follow the instruction; the
        rest keep their original plan (the fallback for non-compliers).
        """
        itype = intervention.get("intervention_type")
        targets = list(intervention.get("target_entity_ids") or [])
        params = dict(intervention.get("_action") or {})
        params.setdefault("targets", targets)
        until = None if duration_sec is None else self._elapsed_sec + float(duration_sec)
        if itype == "event_delay":
            until = None
        mod = self._new_modifier(
            "intervention", itype, params, compliance=float(clamp(compliance, 0.0, 1.0)),
            until_sec=until, intervention_id=intervention.get("intervention_id"),
        )
        if itype == "accommodation_rebalance":
            self._rebalance_rooms(mod)
        if itype == "event_delay":
            self._allocate_rooms()
        self._on_modifiers_changed(mod)
        return mod["modifier_id"]

    def set_compliance(self, compliance: float) -> None:
        """Observed attendee compliance changed: every active intervention adopts it."""
        changed = False
        for m in self._modifiers:
            if m["source"] == "intervention" and m["compliance"] is not None:
                m["compliance"] = float(clamp(compliance, 0.0, 1.0))
                changed = True
        if changed:
            self._rebuild()

    def apply_relief(self, entity_ids: list[str], relief_fraction: float) -> None:
        """Legacy entry point: divert `relief_fraction` of each target's demand to
        its best substitute (never deletes demand)."""
        src = [e for e in entity_ids if e in self.nodes]
        if not src:
            return
        self.apply_intervention(
            {"intervention_type": "reroute_transport", "target_entity_ids": src,
             "_action": {"fraction": relief_fraction}},
            compliance=1.0,
        )

    def _on_modifiers_changed(self, mod: dict | None) -> None:
        self._rebuild()
        if mod is not None and mod["kind"] in ("attendance_delta", "concurrent_event", "event_delay", "event_cancellation"):
            self._allocate_rooms()
        if mod is not None and mod["kind"] in ("gate_closure", "transport_outage", "station_closure"):
            self._requeue_closed()

    def _rebuild(self) -> None:
        """Recompute effective parameters from the active modifier list."""
        eff: dict[str, Any] = {
            "closed": set(), "cap_mult": {}, "weight_mult": {}, "attendance_mult": {},
            "event_shift_min": {}, "extra_events": [], "dwell_mult": 1.0,
            "diversions": [], "stagger": {}, "mu_boost": {}, "coupling_cut": {},
            "room_mult": {}, "hotel_avoid": {}, "cancelled_events": set(),
        }
        now = self._elapsed_sec
        for m in self._modifiers:
            if m.get("until_sec") is not None and now >= m["until_sec"]:
                continue
            k, p = m["kind"], m["params"]
            eid = p.get("entity_id")
            delta = float(p.get("delta_pct", 0.0)) / 100.0
            c = m["compliance"] if m["compliance"] is not None else 1.0
            targets = p.get("targets", [])
            if k == "attendance_delta":
                key = p.get("event_id") or "*"
                eff["attendance_mult"][key] = eff["attendance_mult"].get(key, 1.0) * (1.0 + delta)
            elif k in ("metro_capacity_delta", "road_capacity_delta", "parking_loss"):
                if eid in self.nodes:
                    eff["cap_mult"][eid] = eff["cap_mult"].get(eid, 1.0) * max(0.05, 1.0 + delta)
                    if k == "metro_capacity_delta":
                        # Fewer trains: fewer riders choose that line/station.
                        eff["weight_mult"][eid] = eff["weight_mult"].get(eid, 1.0) * max(0.05, 1.0 + delta)
            elif k == "weather_rain":
                eff["dwell_mult"] *= RAIN_FACTOR.get(str(p.get("intensity", "moderate")), 1.12)
            elif k in ("gate_closure", "transport_outage", "road_closure", "station_closure"):
                if eid in self.nodes:
                    eff["closed"].add(eid)
            elif k == "capacity_reduction":
                # Generic capacity cut on any entity (staffing, partial closure, works).
                if eid in self.nodes:
                    eff["cap_mult"][eid] = eff["cap_mult"].get(eid, 1.0) * max(0.05, 1.0 + delta)
            elif k == "event_cancellation":
                eff["cancelled_events"].add(p.get("event_id", "evt_demo"))
            elif k == "hotel_shortage":
                frac = float(p.get("rooms_offline_pct", abs(delta) * 100.0 if delta else 15.0)) / 100.0
                for h, t in self.types.items():
                    if t == "hotel" and (eid in (None, h)):
                        eff["room_mult"][h] = eff["room_mult"].get(h, 1.0) * max(0.1, 1.0 - frac)
            elif k == "concurrent_event":
                eff["extra_events"].append(p["_event"])
            elif k == "event_delay":
                ev_id = p.get("event_id", "evt_demo")
                minutes = float(p.get("delay_min", float(p.get("delay_sec", 1800)) / 60.0))
                eff["event_shift_min"][ev_id] = eff["event_shift_min"].get(ev_id, 0.0) + minutes
            # --- interventions -----------------------------------------------------
            elif k in ("reroute_transport", "parking_redistribution"):
                src, dst = p.get("source"), p.get("destination")
                if src is None and targets:
                    src = targets[0]
                    dst = targets[1] if len(targets) > 1 else self._best_substitute(src)
                if src and dst:
                    eff["diversions"].append(("access", src, dst, float(p.get("fraction", 0.4)) * c))
            elif k == "transport_redistribution":
                src = p.get("source") or (targets[0] if targets else None)
                shares = p.get("destinations") or [[d, 1.0] for d in targets[1:]]
                tot = sum(float(s) for _, s in shares) or 1.0
                for dst, s in shares:
                    if src and dst:
                        eff["diversions"].append(("access", src, dst, float(p.get("fraction", 0.4)) * c * float(s) / tot))
            elif k == "gate_redistribution":
                hot = p.get("sources") or targets[:-1]
                cool = p.get("destination") or (targets[-1] if targets else None)
                for g in hot:
                    if cool and g != cool:
                        eff["diversions"].append(("gate", g, cool, float(p.get("fraction", 0.3)) * c))
            elif k == "zone_incentive":
                src = p.get("source") or (targets[0] if targets else None)
                dst = p.get("destination") or (targets[1] if len(targets) > 1 else None)
                if src and dst:
                    eff["diversions"].append(("zone", src, dst, float(p.get("fraction", 0.35)) * c))
            elif k == "stagger_entry":
                for g in p.get("gates") or targets:
                    eff["stagger"][g] = (float(p.get("fraction", 0.35)) * c, float(p.get("delay_min", 12.0)))
            elif k == "deploy_shuttle":
                node = p.get("node") or (targets[0] if targets else None)
                if node:
                    eff["mu_boost"][node] = eff["mu_boost"].get(node, 0.0) + float(p.get("capacity_per_min", 40.0))
            elif k == "emergency_corridor":
                for r in p.get("roads") or targets:
                    eff["coupling_cut"][r] = 0.5
            elif k == "accommodation_rebalance":
                src = p.get("source") or (targets[0] if targets else None)
                if src:
                    eff["hotel_avoid"][src] = 1.5 * c
        self._eff = eff

    def _best_substitute(self, eid: str) -> str | None:
        subs = sorted(self.substitutes.get(eid, []), key=lambda s: -s[1])
        return subs[0][0] if subs else None

    def _requeue_closed(self) -> None:
        """People queued at a gate/station that just closed move to open alternatives."""
        for c in list(self._eff["closed"]):
            q = self._queue.get(c, 0.0)
            if q <= 0:
                continue
            self._queue[c] = 0.0
            alts: list[str] = []
            if self.types.get(c) == "gate":
                for v, gates in self.venue_gates.items():
                    if c in gates:
                        alts = [g for g in gates if g not in self._eff["closed"]]
            else:
                alts = [s for s, _ in self.substitutes.get(c, []) if s not in self._eff["closed"]]
            if alts:
                for a in alts:
                    self._queue[a] = self._queue.get(a, 0.0) + q / len(alts)

    # --- capacity -------------------------------------------------------------------------
    def capacity(self, entity_id: str) -> float:
        mult = self._eff["cap_mult"].get(entity_id, 1.0)
        if self.types.get(entity_id) == "hotel":
            mult *= self._eff["room_mult"].get(entity_id, 1.0)
        return max(1.0, self.cap[entity_id] * mult)

    # --- accommodation ------------------------------------------------------------------------
    def _room_demand(self) -> tuple[float, dict[str, float]]:
        need = float(self.hosp["room_need_share"])
        gpr = float(self.hosp["guests_per_room"])
        per = {}
        for ev in self._events():
            if ev["cancelled"]:
                continue
            per[ev["event_id"]] = ev["attendance"] * ev["oot"] * need / gpr
        return sum(per.values()), per

    def _property_rooms(self, p: dict) -> float:
        return p["rooms_total"] * self._eff["room_mult"].get(p["cluster_entity_id"], 1.0)

    def property_available(self, p: dict) -> float:
        occupied = p["base_occupancy"] * p["rooms_total"] + self._event_rooms[p["property_id"]]
        return max(0.0, self._property_rooms(p) - occupied)

    def _main_venue(self) -> str:
        evs = self._events()
        best = max(evs, key=lambda e: e["attendance"], default=None)
        return best["venue"] if best else "stadium_main"

    def _allocate(self, rooms: float, exclude: set[str] | None = None, prefer: set[str] | None = None) -> float:
        """Place `rooms` bookings across properties by logit preference, capped by
        availability. Returns the rooms that could not be placed."""
        if rooms <= 0 or not self.properties:
            return max(0.0, rooms)
        exclude = exclude or set()
        venue = self._main_venue()
        prices = [p["price_per_night_paise"] for p in self.properties]
        pmin, pmax = min(prices), max(prices)
        times = [self._travel_to_venue[p["property_id"]].get(venue, 3600) for p in self.properties]
        tmin, tmax = min(times), max(times)
        avoid = self._eff["hotel_avoid"]
        remaining = rooms
        for _ in range(6):
            weights = {}
            for p in self.properties:
                pid = p["property_id"]
                if pid in exclude or (prefer and p["cluster_entity_id"] not in prefer):
                    continue
                avail = self.property_available(p)
                if avail < 0.5:
                    continue
                price_n = (p["price_per_night_paise"] - pmin) / max(pmax - pmin, 1)
                t = self._travel_to_venue[pid].get(venue, 3600)
                time_n = (t - tmin) / max(tmax - tmin, 1)
                u = -1.1 * price_n - 1.3 * time_n + 0.5 * TIER_SCORE.get(p["tier"], 0.3)
                u -= avoid.get(p["cluster_entity_id"], 0.0)
                weights[pid] = math.exp(2.0 * u)
            total = sum(weights.values())
            if total <= 0 or remaining < 0.5:
                break
            placed = 0.0
            for pid, w in weights.items():
                p = self._props_by_id[pid]
                take = min(self.property_available(p), remaining * w / total)
                self._event_rooms[pid] += take
                placed += take
            remaining -= placed
            if placed < 1e-6:
                break
        return max(0.0, remaining)

    def _allocate_rooms(self, initial: bool = False) -> None:
        """Bring bookings in line with demand-to-date (called each step and on change)."""
        total, _ = self._room_demand()
        t_min = self._elapsed_sec / 60.0
        pre = float(self.hosp["prebooked_share"])
        window = max(1.0, float(self.hosp["booking_window_min"]))
        target = total * (pre + (1.0 - pre) * min(1.0, t_min / window))
        new = target - self._requested_rooms
        if new > 0.5:
            self._requested_rooms += new
            self._unmet_rooms += self._allocate(new)
        elif new < -0.5:
            # Demand fell (cancellation / attendance cut): release bookings pro rata.
            self._requested_rooms += new
            held = sum(self._event_rooms.values())
            if held > 0:
                scale = max(0.0, (held + new) / held)
                for pid in self._event_rooms:
                    self._event_rooms[pid] *= scale
        # Supply shrank below what is booked: displaced guests are re-placed or lost.
        for p in self.properties:
            over = p["base_occupancy"] * p["rooms_total"] + self._event_rooms[p["property_id"]] - self._property_rooms(p)
            if over > 0.5:
                moved = min(over, self._event_rooms[p["property_id"]])
                self._event_rooms[p["property_id"]] -= moved
                self._displaced_rooms += moved
                self._unmet_rooms += self._allocate(moved, exclude={p["property_id"]})

    def _rebalance_rooms(self, mod: dict) -> None:
        """accommodation_rebalance: guests at a saturated cluster accept a transfer
        (with credit) to the alternative cluster, if it has rooms."""
        p = mod["params"]
        targets = p.get("targets", [])
        src = p.get("source") or (targets[0] if targets else None)
        dst = p.get("destination") or (targets[1] if len(targets) > 1 else None)
        if not src:
            return
        frac = float(p.get("fraction", 0.25)) * float(mod["compliance"] or 1.0)
        src_props = [q for q in self.properties if q["cluster_entity_id"] == src]
        moving = sum(self._event_rooms[q["property_id"]] for q in src_props) * frac
        if moving <= 0:
            return
        prefer = {dst} if dst else None
        # Only move what the destination can actually absorb.
        capacity = sum(self.property_available(q) for q in self.properties
                       if (prefer is None or q["cluster_entity_id"] in prefer) and q["cluster_entity_id"] != src)
        moving = min(moving, capacity)
        for q in src_props:
            share = self._event_rooms[q["property_id"]] / max(sum(self._event_rooms[x["property_id"]] for x in src_props), 1e-9)
            self._event_rooms[q["property_id"]] -= moving * share
        left = self._allocate(moving, exclude={q["property_id"] for q in src_props}, prefer=prefer)
        if left > 0:  # nowhere else to go after all: they stay put
            for q in src_props:
                self._event_rooms[q["property_id"]] += left / max(len(src_props), 1)
        mod["params"]["rooms_moved"] = round(moving - left, 1)

    def _cluster_guests(self) -> dict[str, float]:
        gpr = float(self.hosp["guests_per_room"])
        out: dict[str, float] = {}
        for p in self.properties:
            c = p["cluster_entity_id"]
            out[c] = out.get(c, 0.0) + self._event_rooms[p["property_id"]] * gpr
        return out

    # --- background city load -------------------------------------------------------------------
    def _background(self, eid: str, t_min: float) -> float:
        base = BG_UTIL.get(self.types[eid], 0.0)
        if base <= 0:
            return 0.0
        jitter = 0.7 + 0.6 * stable_unit(self.seed, "bg", eid)
        rush = float(self.demand["rush_amplitude"]) * math.exp(
            -0.5 * ((t_min - float(self.demand["rush_peak_elapsed_min"])) / float(self.demand["rush_sd_min"])) ** 2
        )
        return base * jitter * (1.0 + rush)

    # --- the step ---------------------------------------------------------------------------------
    def _split(self, weights: dict[str, float]) -> dict[str, float]:
        total = sum(w for w in weights.values() if w > 0)
        if total <= 0:
            return {}
        return {k: w / total for k, w in weights.items() if w > 0}

    def _divert(self, stage: str, loads: dict[str, float]) -> None:
        for st, src, dst, frac in self._eff["diversions"]:
            if st != stage or src not in loads or dst in self._eff["closed"]:
                continue
            moved = loads[src] * clamp(frac, 0.0, 0.95)
            if moved <= 0:
                continue
            loads[src] -= moved
            loads[dst] = loads.get(dst, 0.0) + moved
            self._stats["diverted_people"] += moved * self._dt_min

    def _access_loads(self, people: float, cluster_guests: dict[str, float], hotel_share: float) -> dict[str, float]:
        """Split one event's movers (people/min) over access nodes by mode."""
        closed, wm = self._eff["closed"], self._eff["weight_mult"]
        loads: dict[str, float] = {}

        def add(a: str, v: float) -> None:
            if v > 0:
                loads[a] = loads.get(a, 0.0) + v

        # Hotel guests: from each cluster to its transport node(s).
        hotel_people = people * hotel_share
        cshare = self._split(cluster_guests)
        for c, s in cshare.items():
            acc = [(a, co) for a, co, _ in self.hotel_access.get(c, []) if a not in closed]
            if not acc:
                acc = [(sub, 1.0) for sub, _ in self.substitutes.get(self.hotel_access.get(c, [("", 0, 0)])[0][0], [])
                       if sub not in closed]
            for a, w in self._split(dict(acc)).items():
                add(a, hotel_people * s * w)

        local = people - hotel_people
        split = self.demand["local_mode_split"]
        # Metro: lines by capacity, stations within a line by feed coefficient.
        line_w = {l: self.capacity(l) * wm.get(l, 1.0) for l in self.lines if l not in closed}
        metro = local * float(split.get("metro", 0.5))
        leftover = 0.0
        for l, s in self._split(line_w).items():
            stn = {st: co * wm.get(st, 1.0) for st, co in self.lines[l]}
            for st in list(stn):
                if st in closed:
                    w = stn.pop(st)
                    for sub, sv in self.substitutes.get(st, []):
                        if sub not in closed:
                            stn[sub] = stn.get(sub, 0.0) + w * sv
            ss = self._split(stn)
            if not ss:
                leftover += metro * s
            for st, w in ss.items():
                add(st, metro * s * w)
        if not line_w:
            leftover += metro
        bus = local * float(split.get("bus", 0.15)) + leftover
        for h, w in self._split({h: self.capacity(h) * wm.get(h, 1.0) for h in self.bus_hubs if h not in closed}).items():
            add(h, bus * w)
        shuttle = local * float(split.get("shuttle", 0.10))
        for h, w in self._split({h: self.capacity(h) * wm.get(h, 1.0) for h in self.shuttle_hubs if h not in closed}).items():
            add(h, shuttle * w)
        car = local * float(split.get("car", 0.25))
        lot_w = {}
        for p in self.lots:
            if p in closed:
                continue
            fill = self._parked.get(p, 0.0) / self.capacity(p)
            lot_w[p] = self.capacity(p) * wm.get(p, 1.0) * (1.0 if fill < PARKING_FULL else 0.02)
        for p, w in self._split(lot_w).items():
            add(p, car * w)
        self._divert("access", loads)
        return loads

    def _gate_choice(self, venue: str, access: str, lam: float, flows: dict, path_acc: list) -> None:
        opts = self.options.get(venue, {}).get(access)
        closed = self._eff["closed"]
        theta = float(self.demand["route_sensitivity"])
        if opts:
            opts = [o for o in opts if o[0] is None or o[0] not in closed]
        if not opts:
            gates = [g for g in self.venue_gates.get(venue, []) if g not in closed]
            opts = [(g, 0.1, 600) for g in gates]
        if not opts:
            flows["direct"][venue] = flows["direct"].get(venue, 0.0) + lam
            return
        w = {}
        for g, co, tt in opts:
            key = g or f"direct::{venue}"
            pen = 0.0 if g is None else max(0.0, self._util_prev.get(g, 0.0) - 0.7) * 4.0
            w[key] = (w.get(key, 0.0) + co * math.exp(-theta * pen), tt)
        tot = sum(x[0] for x in w.values())
        for key, (wt, tt) in w.items():
            part = lam * wt / tot
            if key.startswith("direct::"):
                flows["direct"][venue] = flows["direct"].get(venue, 0.0) + part
            else:
                flows["gate_in"][key] = flows["gate_in"].get(key, 0.0) + part
            path_acc.append((access, None if key.startswith("direct::") else key, part, tt))

    def _step(self, dt_sec: float) -> None:
        dt = dt_sec / 60.0
        self._dt_min = max(dt, 1e-9)
        t = self._elapsed_sec / 60.0
        eff = self._eff
        closed = eff["closed"]
        d = self.demand
        cluster_guests = self._cluster_guests()
        total_guests = sum(cluster_guests.values())
        room_total, room_per_event = self._room_demand()

        flows = {"acc_in": {}, "acc_out": {}, "gate_in": {}, "gate_out": {}, "direct": {}, "line": {},
                 "car_in": {}, "car_out": {}, "boarding": {}}
        path_acc: list[tuple[str, str | None, float, int]] = []
        egress_by_venue: dict[str, float] = {}
        flow_view: dict[str, dict[str, float | None]] = {}

        for ev in self._events():
            st = self._ev_state.setdefault(ev["event_id"], {"arrived": 0.0, "egressed": 0.0, "inside": 0.0, "split": {}, "queued": 0.0})
            A = ev["attendance"]
            sd = ev["arr_sd"]
            phi = _phi((t - ev["arr_mid"]) / sd)
            curve = (round(ev["arr_mid"], 3), round(sd, 3))
            if dt_sec == 0.0:
                st["curve_seen"], st["anchor"] = curve, None
            elif st.get("curve_seen") is None:
                # Created mid-run: its visitors arrive over what is left of its window.
                st["curve_seen"], st["anchor"] = curve, (st["arrived"], phi)
            elif curve != st["curve_seen"]:
                # Rescheduled mid-arrival: whoever has arrived stays; only the
                # people still to come follow the new schedule from now on.
                st["curve_seen"] = curve
                st["anchor"] = (st["arrived"], phi)
            if st.get("anchor") and st["anchor"][1] < 0.999:
                a0, p0 = st["anchor"]
                target = a0 + max(0.0, A - a0) * clamp((phi - p0) / (1.0 - p0), 0.0, 1.0)
            else:
                target = A * phi
            if dt_sec == 0.0:
                # t=0 snapshot: people who arrived before sim start are already inside.
                pre = target - st["arrived"]
                if pre > 0:
                    st["arrived"] += pre
                    # Early arrivals are inside only up to the venue's capacity;
                    # the rest wait outside (never counted as occupancy).
                    v = ev["venue"]
                    inside_v = sum(x.get("inside", 0.0) for x in self._ev_state.values() if x.get("venue") == v)
                    admit = min(pre, max(0.0, self.capacity(v) - inside_v))
                    st["inside"] += admit
                    if pre > admit:
                        self._queue[v] = self._queue.get(v, 0.0) + (pre - admit)
                    # Record how they came, so their egress loads the right paths.
                    scratch = {"gate_in": {}, "direct": {}}
                    guests_e = total_guests * (room_per_event.get(ev["event_id"], 0.0) / room_total if room_total > 0 else 0.0)
                    h = min(0.9, guests_e / A) if A > 0 else 0.0
                    for a, lam in self._access_loads(pre, cluster_guests, h).items():
                        via = a if a in self.options.get(ev["venue"], {}) else None
                        if via is None:
                            cands = list(self.options.get(ev["venue"], {}))
                            if not cands:
                                continue
                            via = max(cands, key=lambda x: self.capacity(x))
                        acc: list = []
                        self._gate_choice(ev["venue"], via, lam, scratch, acc)
                        for a0, g0, part, _ in acc:
                            st["split"][(a0, g0)] = st["split"].get((a0, g0), 0.0) + part
                arr_rate = 0.0
            else:
                max_rate = A * 2.5 / (sd * 2.5066) if A > 0 else 0.0
                arr = clamp(target - st["arrived"], 0.0, max_rate * dt)
                floor = getattr(self, "_arrival_floor", None)
                if floor:
                    # Redoing a step: whoever already arrived in it still arrives
                    # (a change never removes people who are already here).
                    arr = max(arr, floor.get(ev["event_id"], 0.0) - st["arrived"])
                st["arrived"] += arr
                arr_rate = arr / dt
            # Egress: after the event ends, or immediately if cancelled.
            # A cancelled event ends when it was cancelled (recorded once), and
            # people leave promptly; a restored event returns to its schedule.
            if ev["cancelled"]:
                st.setdefault("cancel_min", t)
                # Evacuation-style departure: a steady outflow over 20 minutes,
                # not a wave that is already half gone at the moment of cancellation.
                eg_frac = clamp((t - st["cancel_min"]) / CANCEL_EGRESS_MIN, 0.0, 1.0)
            else:
                st.pop("cancel_min", None)
                eg_frac = _phi((t - ev["dep_mid"]) / ev["dep_sd"])
            st["eg_frac"] = eg_frac
            eg_target = eg_frac * st["arrived"]
            eg = clamp(eg_target - st["egressed"], 0.0, st["inside"]) if dt_sec > 0 else 0.0
            st["egressed"] += eg
            st["inside"] -= eg
            eg_rate = eg / dt if dt_sec > 0 else 0.0
            egress_by_venue[ev["venue"]] = egress_by_venue.get(ev["venue"], 0.0) + eg_rate

            if arr_rate > 0:
                guests_e = total_guests * (room_per_event.get(ev["event_id"], 0.0) / room_total if room_total > 0 else 0.0)
                h = min(0.9, guests_e / A) if A > 0 else 0.0
                loads = self._access_loads(arr_rate, cluster_guests, h)
                for a, lam in loads.items():
                    flows["acc_in"][a] = flows["acc_in"].get(a, 0.0) + lam
                    if a in self.station_line:
                        l = self.station_line[a]
                        flows["line"][l] = flows["line"].get(l, 0.0) + lam
                    if self.types.get(a) == "parking":
                        flows["car_in"][a] = flows["car_in"].get(a, 0.0) + lam
                    if ev["venue"] not in self.options or a not in self.options[ev["venue"]]:
                        # No direct option from this node: transfer by metro to one that has.
                        flows["boarding"][a] = flows["boarding"].get(a, 0.0) + lam
                        alt = [x for x in self.options.get(ev["venue"], {}) if x not in closed]
                        alt_w = self._split({x: self.capacity(x) for x in alt if self.types.get(x) == "transport_node"} or {x: 1.0 for x in alt})
                        for x, w in alt_w.items():
                            before = len(path_acc)
                            self._gate_choice(ev["venue"], x, lam * w, flows, path_acc)
                            for i in range(before, len(path_acc)):
                                a0, g0, part, tt = path_acc[i]
                                path_acc[i] = (a0, g0, part, tt + 600)
                                st["split"][(x, g0)] = st["split"].get((x, g0), 0.0) + part * dt
                            flows["acc_in"][x] = flows["acc_in"].get(x, 0.0) + lam * w
                    else:
                        before = len(path_acc)
                        self._gate_choice(ev["venue"], a, lam, flows, path_acc)
                        for i in range(before, len(path_acc)):
                            a0, g0, part, _ = path_acc[i]
                            st["split"][(a0, g0)] = st["split"].get((a0, g0), 0.0) + part * dt
            if eg_rate > 0 and st["split"]:
                tot = sum(st["split"].values())
                for (a, g), n in st["split"].items():
                    lam = eg_rate * n / tot
                    flows["acc_out"][a] = flows["acc_out"].get(a, 0.0) + lam
                    if g:
                        flows["gate_out"][g] = flows["gate_out"].get(g, 0.0) + lam
                    if a in self.station_line:
                        l = self.station_line[a]
                        flows["line"][l] = flows["line"].get(l, 0.0) + lam
                    if self.types.get(a) == "parking":
                        flows["car_out"][a] = flows["car_out"].get(a, 0.0) + lam
            st["venue"] = ev["venue"]
            st["start"] = ev["start"]

        self._divert("gate", flows["gate_in"])
        # Staggered entry: part of each affected gate's inflow is held and released later.
        for g, (frac, delay) in eff["stagger"].items():
            lam = flows["gate_in"].get(g, 0.0)
            hold = lam * clamp(frac, 0.0, 0.9)
            release = self._held.get(g, 0.0) / max(delay, 1.0)
            self._held[g] = self._held.get(g, 0.0) + (hold - release) * dt
            flows["gate_in"][g] = lam - hold + release
            self._stats["diverted_people"] += hold * dt
        for g in list(self._held):
            if g not in eff["stagger"] and self._held[g] > 0:
                flows["gate_in"][g] = flows["gate_in"].get(g, 0.0) + self._held[g] / 6.0
                self._held[g] *= max(0.0, 1.0 - dt / 6.0)

        counts: dict[str, float] = {}
        dm = eff["dwell_mult"]
        mu_boost = eff["mu_boost"]

        # Stations and hubs (fluid queue on the station's service rate).
        for a in self.station_line.keys() | set(self.bus_hubs) | set(self.shuttle_hubs):
            bg = self._background(a, t) * self.capacity(a)
            if a in closed:
                counts[a] = 0.0
                self._queue[a] = 0.0
                continue
            lam = flows["acc_in"].get(a, 0.0) + flows["acc_out"].get(a, 0.0)
            mu = self.capacity(a) * STATION_MU_PER_CAP / dm + mu_boost.get(a, 0.0)
            q_prev = self._queue.get(a, 0.0)
            q = max(0.0, q_prev + (lam - mu) * dt)
            self._queue[a] = q
            # Queue balance: served = arrivals - growth of the queue.
            flow_view[a] = {"inflow_per_min": lam, "outflow_per_min": max(0.0, lam - (q - q_prev) / dt) if dt_sec > 0 else min(lam, mu),
                            "queue_people": q}
            counts[a] = min(
                bg + min(lam, mu) * STATION_DWELL_MIN * dm + q + flows["boarding"].get(a, 0.0) * BOARDING_DWELL_MIN,
                STATION_MAX * self.capacity(a),
            )

        # Lines: hourly load against hourly capacity.
        for l in self.lines:
            if l in closed:
                counts[l] = 0.0
                continue
            counts[l] = min(self._background(l, t) * self.capacity(l) + flows["line"].get(l, 0.0) * 60.0,
                            LINE_MAX * self.capacity(l))
            flow_view[l] = {"inflow_per_min": flows["line"].get(l, 0.0), "outflow_per_min": flows["line"].get(l, 0.0),
                            "queue_people": None}

        # Gates: arrivals queue on the scan rate; entries fill the venue.
        venue_inside: dict[str, float] = {}
        for ev_id, st in self._ev_state.items():
            if "venue" in st:
                venue_inside[st["venue"]] = venue_inside.get(st["venue"], 0.0) + st["inside"]
        spill: dict[str, float] = {}
        entries_by_venue: dict[str, float] = {}
        gate_venue = {g: v for v, gs in self.venue_gates.items() for g in gs}
        # A venue never holds more than its capacity: gates admit only into the
        # room that is left; everyone else waits in the gate queue (outside).
        room = {v: max(0.0, self.capacity(v) - venue_inside.get(v, 0.0)) for v in self.venues}
        # Nobody is admitted to an event that is already emptying out.
        admitting = {st.get("venue") for st in self._ev_state.values() if st.get("eg_frac", 0.0) < ENTRY_CLOSE_EGRESS}
        for v in room:
            if v not in admitting:
                room[v] = 0.0
        for g in (e for e, ty in self.types.items() if ty == "gate"):
            v = gate_venue.get(g)
            full = v is not None and room.get(v, 1.0) <= 0.0
            mu = 0.0 if (g in closed or full) else self.capacity(g) * GATE_MU_PER_CAP / dm + mu_boost.get(g, 0.0)
            q_in = flows["gate_in"].get(g, 0.0)
            q = self._queue.get(g, 0.0) + q_in * dt
            served = min(q, mu * dt, room[v]) if v in room else min(q, mu * dt)
            if v in room:
                room[v] -= served
            q -= served
            flow_view[g] = {"inflow_per_min": q_in + flows["gate_out"].get(g, 0.0),
                            "outflow_per_min": (served / dt if dt_sec > 0 else 0.0) + flows["gate_out"].get(g, 0.0),
                            "queue_people": q}
            self._queue[g] = q
            if v:
                entries_by_venue[v] = entries_by_venue.get(v, 0.0) + served
            hold = self.capacity(g) * GATE_HOLD_FACTOR
            gc = q + (served / dt if dt > 0 else 0.0) * GATE_DWELL_MIN + flows["gate_out"].get(g, 0.0) * 1.0
            if gc > hold:
                spill[g] = gc - hold
                gc = hold
            counts[g] = 0.0 if g in closed else gc
        # Direct approaches (no scanned gate): people wait outside when the venue is full.
        outside = {v for v in self.venues if self._queue.get(v, 0.0) > 0}
        for v in sorted(set(flows["direct"]) | outside):
            n = flows["direct"].get(v, 0.0)
            waiting = self._queue.get(v, 0.0) + n * dt
            admit = min(waiting, room.get(v, waiting))
            if v in room:
                room[v] -= admit
            self._queue[v] = waiting - admit
            entries_by_venue[v] = entries_by_venue.get(v, 0.0) + admit

        # Distribute entries back to the events at each venue (proportional to queued demand).
        for ev_id, st in self._ev_state.items():
            v = st.get("venue")
            if v is None:
                continue
            if dt_sec > 0:
                # In transit or queued: arrived but neither inside nor gone.
                st["_pending"] = max(0.0, st["arrived"] - st["egressed"] - st["inside"])
        for v, served in entries_by_venue.items():
            evs = [s for s in self._ev_state.values() if s.get("venue") == v]
            pend = sum(s.get("_pending", 0.0) for s in evs)
            for s in evs:
                share = s.get("_pending", 0.0) / pend if pend > 0 else 1.0 / len(evs)
                took = min(served * share, s.get("_pending", 0.0)) if pend > 0 else 0.0
                s["inside"] += took
                if t > s.get("start", 1e9):
                    self._stats["late_entries"] += took
                self._stats["entered"] += took

        # People still outside when their event is over (or cancelled) go home:
        # they leave the queues and are accounted as departed, never deleted.
        if dt_sec > 0:
            for st in self._ev_state.values():
                v = st.get("venue")
                pending = st["arrived"] - st["inside"] - st["egressed"]
                if v is None or pending <= 1e-6 or st.get("eg_frac", 0.0) < ENTRY_CLOSE_EGRESS:
                    continue
                give = pending * min(1.0, dt / 10.0)
                st["egressed"] += give
                left = give
                for key in [v] + list(self.venue_gates.get(v, [])):
                    take = min(self._queue.get(key, 0.0), left)
                    if take > 0:
                        self._queue[key] -= take
                        left -= take

        for v in self.venues:
            if self.types[v] == "venue":
                counts[v] = sum(s["inside"] for s in self._ev_state.values() if s.get("venue") == v)
            flow_view[v] = {"inflow_per_min": entries_by_venue.get(v, 0.0) / dt if dt_sec > 0 else 0.0,
                            "outflow_per_min": egress_by_venue.get(v, 0.0),
                            "queue_people": self._queue.get(v, 0.0) + sum(self._queue.get(g, 0.0) for g in self.venue_gates.get(v, []))}

        # Parking: vehicles accumulate on arrival and drain on egress.
        ppv = float(d["persons_per_vehicle"])
        for p in self.lots:
            parked = self._parked.get(p, 0.0) + (flows["car_in"].get(p, 0.0) - flows["car_out"].get(p, 0.0)) / ppv * dt
            parked = max(0.0, parked)
            cap = self.capacity(p) - self._background(p, t) * self.capacity(p)
            if parked > cap:
                self._stats["unparked_people"] += (parked - cap) * ppv
                parked = max(cap, 0.0)
            self._parked[p] = parked
            counts[p] = 0.0 if p in closed else self._background(p, t) * self.capacity(p) + parked
            flow_view[p] = {"inflow_per_min": flows["car_in"].get(p, 0.0), "outflow_per_min": flows["car_out"].get(p, 0.0),
                            "queue_people": None}

        # Roads: pedestrian approach + car traffic + queue spill, one extra hop.
        first: dict[str, float] = {}
        for src, roads in self.spill_roads.items():
            if self.types[src] == "gate":
                lam = flows["gate_in"].get(src, 0.0) + flows["gate_out"].get(src, 0.0)
            else:
                lam = flows["direct"].get(src, 0.0)
            tot = sum(c for _, c in roads) or 1.0
            for r, c in roads:
                first[r] = first.get(r, 0.0) + lam * c * ROAD_PED_SHARE * ROAD_DWELL_MIN * dm + spill.get(src, 0.0) * c / tot
        for p, gates in self.lot_gates.items():
            lam = flows["car_in"].get(p, 0.0) + flows["car_out"].get(p, 0.0)
            for g, _ in gates:
                for r, c in self.spill_roads.get(g, []):
                    first[r] = first.get(r, 0.0) + lam / ppv * 1.5 * c * ROAD_DWELL_MIN * dm
        roads = [e for e, ty in self.types.items() if ty == "road"]
        for r in roads:
            if r in closed and first.get(r, 0.0) > 0:
                nbrs = [(n, c) for n, c in self.road_neighbours.get(r, []) if n not in closed]
                tot = sum(c for _, c in nbrs)
                for n, c in nbrs:
                    first[n] = first.get(n, 0.0) + first[r] * c / tot
                first[r] = 0.0
        for r in roads:
            second = sum(first.get(n, 0.0) * c * 0.5 for n, c in self.road_neighbours.get(r, []))
            counts[r] = 0.0 if r in closed else self._background(r, t) * self.capacity(r) + first.get(r, 0.0) + second
        overflow = {r: max(0.0, counts[r] - ROAD_MAX * self.capacity(r)) for r in roads}
        for r, over in overflow.items():
            if over <= 0:
                continue
            counts[r] = ROAD_MAX * self.capacity(r)
            nbrs = [(n, c) for n, c in self.road_neighbours.get(r, []) if n not in closed]
            tot = sum(c for _, c in nbrs) or 1.0
            for n, c in nbrs:
                counts[n] = counts.get(n, 0.0) + over * c / tot
        for r in roads:
            counts[r] = min(counts[r], ROAD_MAX * self.capacity(r))
            # Pass-through: occupancy = flow x dwell (Little's law), so flow = event load / dwell.
            lam_r = max(0.0, counts[r] - self._background(r, t) * self.capacity(r)) / (ROAD_DWELL_MIN * dm)
            flow_view[r] = {"inflow_per_min": lam_r, "outflow_per_min": lam_r, "queue_people": None}

        # Zones: people arriving on foot from stations/hubs, crowds around queued gates.
        zone_add: dict[str, float] = {}
        for a, zones in self.access_zones.items():
            lam = flows["acc_in"].get(a, 0.0) + flows["acc_out"].get(a, 0.0)
            for z, c in zones:
                zone_add[z] = zone_add.get(z, 0.0) + lam * c * ZONE_DWELL_MIN * dm
        for g, zones in self.gate_zones.items():
            for z, c in zones:
                zone_add[z] = zone_add.get(z, 0.0) + min(self._queue.get(g, 0.0), self.capacity(g)) * c * 0.8
        self._divert("zone", zone_add)
        for z in (e for e, ty in self.types.items() if ty == "zone"):
            inside = sum(s["inside"] for s in self._ev_state.values() if s.get("venue") == z)
            counts[z] = min(self._background(z, t) * self.capacity(z) + zone_add.get(z, 0.0) + inside,
                            ZONE_MAX * self.capacity(z))
            if z not in flow_view:
                lam_z = zone_add.get(z, 0.0) / (ZONE_DWELL_MIN * dm)
                flow_view[z] = {"inflow_per_min": lam_z, "outflow_per_min": lam_z, "queue_people": None}

        # Hotels: rooms occupied.
        if dt_sec > 0:
            self._allocate_rooms()
        for h in (e for e, ty in self.types.items() if ty == "hotel"):
            counts[h] = sum(
                p["base_occupancy"] * p["rooms_total"] + self._event_rooms[p["property_id"]]
                for p in self.properties if p["cluster_entity_id"] == h
            )

        # Emergency posts: incident load rises with crowding upstream (evacuates_to).
        cut = eff["coupling_cut"]
        for ef in (e for e, ty in self.types.items() if ty == "emergency_facility"):
            load = self._background(ef, t) * self.capacity(ef)
            for src, c in self.evac_in.get(ef, []):
                u = counts.get(src, 0.0) / self.capacity(src)
                load += c * max(0.0, u - EMERGENCY_TRIGGER_UTIL) * self.capacity(ef) * EMERGENCY_GAIN * (1.0 - cut.get(src, 0.0))
            counts[ef] = min(load, EMERGENCY_MAX * self.capacity(ef))

        # Deterministic sensor-scale noise (±1%), keyed on (entity, 30s bucket).
        bucket = int(self._elapsed_sec // 30)
        for e in counts:
            if counts[e] > 0 and self.types[e] not in ("hotel", "venue"):
                counts[e] *= 1.0 + (stable_unit(self.seed, "noise", e, bucket) - 0.5) * 0.02

        # Travel-time bookkeeping: base path time plus queue delays at station and gate.
        tw, tp = 0.0, 0.0
        for a, g, lam, tt in path_acc:
            delay = 0.0
            mu_a = self.capacity(a) * STATION_MU_PER_CAP
            if self._queue.get(a, 0.0) > 0 and mu_a > 0:
                delay += self._queue[a] / mu_a * 60.0
            if g:
                mu_g = self.capacity(g) * GATE_MU_PER_CAP
                if mu_g > 0:
                    delay += self._queue.get(g, 0.0) / mu_g * 60.0
            tw += lam * (tt * dm + delay)
            tp += lam
        if tp > 0:
            self._stats["travel_time_weighted"] += tw * dt
            self._stats["travel_people"] += tp * dt
            self._path_time["current"] = tw / tp

        self._counts = counts
        self._flow_view = {e: {k: (None if v is None else round(float(v), 2)) for k, v in fv.items()}
                           for e, fv in flow_view.items()}
        self._util_prev = {e: c / self.capacity(e) for e, c in counts.items()}
        self._last_flows = {
            "arrivals_per_min": sum(flows["acc_in"].values()),
            "egress_per_min": sum(flows["acc_out"].values()),
        }

    # --- 03 §8.1 interface ----------------------------------------------------------------
    def tick(self, dt_sec: int = 30) -> dict[str, float]:
        """Advance the city one step; return readings from instrumented entities."""
        if getattr(self, "keep_step_snapshot", False):
            self._pre_tick = ({k: copy.deepcopy(getattr(self, k)) for k in _RESTORABLE}, dt_sec)
        self._prev_counts = dict(self._counts)
        self._elapsed_sec += dt_sec
        if any(m.get("until_sec") is not None and self._elapsed_sec - dt_sec < m["until_sec"] <= self._elapsed_sec
               for m in self._modifiers):
            self._rebuild()
        self._step(float(dt_sec))
        return self._observe()

    def _observe(self) -> dict[str, float]:
        bucket = int(self._elapsed_sec // 30)
        dropout = float(self.demand.get("sensor_dropout_rate", 0.0))
        observations: dict[str, float] = {}
        for eid, count in self._counts.items():
            if not self._observed[eid]:
                continue
            if stable_unit(self.seed, "dropout", eid, bucket) < dropout:
                continue  # sensor missed this reading; the twin carries the estimate
            err = (stable_unit(self.seed, "sensor", eid, bucket) - 0.5) * 0.03
            observations[eid] = max(0.0, count * (1.0 + err))
        return observations

    def resimulate_last_step(self) -> dict[str, float] | None:
        """Re-run the most recent step from its starting state under the current
        schedule and modifiers (an operator change applies to the step now in
        progress). The clock ends where it was; nothing is double-counted,
        because every stock (arrivals, queues, occupancy) is rolled back first.
        Returns the new sensor readings, or None when there is no step to redo."""
        snap = getattr(self, "_pre_tick", None)
        if snap is None:
            if self._elapsed_sec == 0:
                self._init_dynamic()      # still at the initial instant: re-derive it
                return self._observe()
            return None
        state, dt = snap
        committed = {e: st.get("arrived", 0.0) for e, st in self._ev_state.items()}
        for k, v in state.items():
            setattr(self, k, copy.deepcopy(v))
        self._rebuild()
        self._arrival_floor = committed
        try:
            return self.tick(dt)
        finally:
            self._arrival_floor = None

    def run(self, seconds: float, step_sec: int = 60) -> None:
        """Advance without collecting observations (what-if / projection use)."""
        steps = max(1, int(round(seconds / step_sec)))
        for _ in range(steps):
            self.tick(step_sec)

    def ground_truth(self) -> dict[str, dict[str, Any]]:
        """Full true state. Evaluation only — never exposed through the API."""
        out: dict[str, dict[str, Any]] = {}
        for eid, count in self._counts.items():
            cap = self.capacity(eid)
            prev = self._prev_counts.get(eid, count)
            out[eid] = {
                "current_count": count,
                "utilisation": count / cap,
                "flow_rate_per_min": (count - prev) * 2.0,
                "is_observed": self._observed[eid],
                "capacity": cap,
            }
        return out

    def utilisation(self) -> dict[str, float]:
        return {e: c / self.capacity(e) for e, c in self._counts.items()}

    def event_venues(self) -> list[str]:
        """Venues visitors can actually reach (at least one station, hub or car
        park leads there); an event anywhere else could never be attended."""
        return [v for v in self.venues if self.options.get(v)]

    def flow_state(self) -> dict[str, dict[str, float | None]]:
        """Per-entity people/min in and out and people queued, from the last step.
        Queued people wait *outside* the entity (gate/station queue, or outside a
        full venue) and are never counted in its occupancy."""
        return {e: dict(v) for e, v in self._flow_view.items()}

    def event_ledger(self) -> dict[str, dict[str, float]]:
        """Person accounting per event: arrived = inside + egressed + pending."""
        return {ev_id: {"arrived": st.get("arrived", 0.0), "inside": st.get("inside", 0.0),
                        "egressed": st.get("egressed", 0.0),
                        "pending": st.get("arrived", 0.0) - st.get("inside", 0.0) - st.get("egressed", 0.0)}
                for ev_id, st in self._ev_state.items()}

    def elapsed_sec(self) -> float:
        return self._elapsed_sec

    def queue_delay_sec(self, eid: str) -> float:
        q = self._queue.get(eid, 0.0)
        if q <= 0 or self.types.get(eid) not in ("gate", "transport_node"):
            return 0.0  # a venue's outside queue is reported as people, not a scan delay
        rate = GATE_MU_PER_CAP if self.types.get(eid) == "gate" else STATION_MU_PER_CAP
        mu = self.capacity(eid) * rate
        return q / mu * 60.0 if mu > 0 else 0.0

    def is_closed(self, eid: str) -> bool:
        return eid in self._eff["closed"]

    def closed_entities(self) -> set[str]:
        """Entities currently out of service (closures, outages)."""
        return set(self._eff["closed"])

    def gates_of_venue(self, venue_id: str) -> list[str]:
        return list(self.venue_gates.get(venue_id, []))

    # --- reporting --------------------------------------------------------------------------
    def properties_state(self) -> list[dict[str, Any]]:
        sat = float(self.hosp["saturation_threshold"])
        lim = float(self.hosp["limited_threshold"])
        out = []
        for p in self.properties:
            rooms = self._property_rooms(p)
            occupied = min(rooms, p["base_occupancy"] * p["rooms_total"] + self._event_rooms[p["property_id"]])
            occ = occupied / rooms if rooms > 0 else 1.0
            out.append({
                **p,
                "rooms_available_total": int(round(rooms)),
                "rooms_occupied": int(round(occupied)),
                "rooms_available": int(max(0, math.floor(rooms - occupied))),
                "occupancy": round(occ, 4),
                "status": "saturated" if occ >= sat else "limited" if occ >= lim else "available",
                "travel_time_to_venue_sec": dict(self._travel_to_venue.get(p["property_id"], {})),
            })
        return out

    def event_states(self) -> dict[str, dict[str, Any]]:
        out = {}
        for ev in self._events():
            st = self._ev_state.get(ev["event_id"], {})
            out[ev["event_id"]] = {
                "attendance": ev["attendance"], "start_min": ev["start"], "end_min": ev["end"],
                "arrived": st.get("arrived", 0.0), "inside": st.get("inside", 0.0),
                "egressed": st.get("egressed", 0.0), "venue": ev["venue"], "cancelled": ev["cancelled"],
            }
        return out

    def stats(self) -> dict[str, float]:
        """Operational aggregates the metrics panel and what-if report."""
        props = self.properties_state()
        queued = sum(q for e, q in self._queue.items() if self.types.get(e) in ("gate", "transport_node"))
        travel = (self._stats["travel_time_weighted"] / self._stats["travel_people"]) if self._stats["travel_people"] > 0 else 0.0
        return {
            "rooms_available": float(sum(p["rooms_available"] for p in props)),
            "rooms_unmet": round(self._unmet_rooms, 1),
            "rooms_displaced": round(self._displaced_rooms, 1),
            "saturated_properties": float(sum(1 for p in props if p["status"] == "saturated")),
            "queued_people": round(queued, 1),
            "late_entries": round(self._stats["late_entries"], 1),
            "unparked_people": round(self._stats["unparked_people"], 1),
            "diverted_people": round(self._stats["diverted_people"], 1),
            "avg_travel_time_sec": round(travel, 1),
            "current_travel_time_sec": round(self._path_time.get("current", 0.0), 1),
            "arrivals_per_min": round(self._last_flows.get("arrivals_per_min", 0.0), 1),
            "egress_per_min": round(self._last_flows.get("egress_per_min", 0.0), 1),
        }

    # --- lifecycle ------------------------------------------------------------------------------
    def reset(self, seed: int | None = None) -> None:
        if seed is not None:
            self.seed = seed
        self._modifiers = []
        self._mod_counter = 0
        self._init_dynamic()

    def seek(self, elapsed_sec: float) -> None:
        """Replay from the start to `elapsed_sec` — the model is stateful, so a seek
        is a deterministic fast-forward, never a jump."""
        mods = [m for m in self._modifiers if m["source"] == "schedule"]
        self._modifiers = mods
        self._init_dynamic()
        while self._elapsed_sec + 30 <= elapsed_sec:
            self.tick(30)

    def clone(self, sources: set[str] | None = None) -> "SyntheticGenerator":
        """Independent copy sharing static structure. `sources` restricts which
        modifier sources the copy keeps (e.g. the twin's nominal model keeps
        interventions and schedule changes but not unannounced disruptions)."""
        new = object.__new__(SyntheticGenerator)
        for k, v in self.__dict__.items():
            new.__dict__[k] = copy.deepcopy(v) if k in _DYNAMIC else v
        if sources is not None:
            new._modifiers = [m for m in new._modifiers if m["source"] in sources]
            new._rebuild()
        new._pre_tick = None               # a copy starts without a step to redo,
        new.keep_step_snapshot = False     # and projection copies never pay for one
        return new

    def generate_cascade_dataset(self, n_scenarios: int = 5000, randomise_topology: bool = True) -> list[dict]:
        """GNN training data is owned by the ML workstream (03 §8.1)."""
        raise NotImplementedError(
            "generate_cascade_dataset belongs to the ML workstream (ml/generator.py); "
            "the backend generator drives the live city."
        )
