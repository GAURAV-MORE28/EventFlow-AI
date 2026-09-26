"""Forward simulation over clones of the live city.

One runner serves three consumers, so they share the live system's physics:

* What-If  (`services/simulation.py`) — baseline copy vs scenario copy.
* Candidate evaluation (`services/evaluation.py`) — each proposed intervention
  applied to its own copy, measured against the do-nothing copy.
* Look-ahead projection (this module) — how the city is expected to evolve
  under the announced schedule; used for departure-time (off-peak) advice,
  journey crowding and "what becomes critical next".

Clones never touch the live generator; cloning itself happens under the
engine's world lock so a clone never observes a half-applied cycle.
"""
from __future__ import annotations

from typing import Any

from ..config import get_config
from ..simtime import parse

SKIP_PEAK_TYPES = {"hotel"}  # occupancy is reported separately, not as crowd pressure


def critical_map(engine: Any) -> dict[str, float]:
    """Per-entity critical line, the same one live risk scoring uses."""
    return {e: engine.config.thresholds_for(n["entity_type"])[1] for e, n in engine.store.nodes.items()}


def run_forward(gen: Any, horizon_sec: float, step_sec: int = 60, sample_sec: int = 300,
                critical: float | dict[str, float] = 0.90) -> dict[str, Any]:
    """Advance `gen` (a clone) and summarise what happened along the way."""
    types = gen.types
    line = critical if isinstance(critical, dict) else {e: float(critical) for e in types}
    start = gen.utilisation()
    # Peaks and critical sets cover the future only: the starting instant is
    # identical in every world being compared, so it cannot show an effect.
    peaks: dict[str, float] = {e: 0.0 for e in start}
    samples: list[dict[str, Any]] = [{"offset_sec": 0, "util": dict(start),
                                      "delay": {e: gen.queue_delay_sec(e) for e in start}}]
    critical_ever: set[str] = set()
    elapsed = 0
    steps = max(1, int(round(horizon_sec / step_sec)))
    for _ in range(steps):
        gen.tick(step_sec)
        elapsed += step_sec
        util = gen.utilisation()
        for e, u in util.items():
            if u > peaks.get(e, 0.0):
                peaks[e] = u
            if u >= line.get(e, 0.9) and types[e] not in SKIP_PEAK_TYPES:
                critical_ever.add(e)
        if elapsed % sample_sec == 0 or elapsed >= steps * step_sec:
            samples.append({"offset_sec": elapsed, "util": dict(util),
                            "delay": {e: gen.queue_delay_sec(e) for e in util}})
    return {"peaks": peaks, "samples": samples, "critical": critical_ever, "stats": gen.stats(),
            "final": gen.utilisation(), "gen": gen}


def summarise_side(result: dict[str, Any], types: dict[str, str]) -> dict[str, Any]:
    """The comparison metrics reported for one side of a what-if."""
    peaks = {e: u for e, u in result["peaks"].items() if types[e] not in SKIP_PEAK_TYPES}
    peak_entity = max(peaks, key=lambda e: peaks[e]) if peaks else None
    final = result["final"]
    zones = [final[e] for e in final if types[e] == "zone"]
    mean_z = sum(zones) / len(zones) if zones else 0.0
    variance = sum((z - mean_z) ** 2 for z in zones) / len(zones) if zones else 0.0

    def mean_peak(kind: set[str]) -> float:
        vals = [u for e, u in peaks.items() if types[e] in kind]
        return round(sum(vals) / len(vals), 4) if vals else 0.0

    hotels = [u for e, u in result["peaks"].items() if types[e] == "hotel"]
    stats = result["stats"]
    return {
        "peak_utilisation": round(peaks[peak_entity], 4) if peak_entity else 0.0,
        "peak_entity_id": peak_entity,
        "load_variance": round(variance, 4),
        "critical_count": len(result["critical"]),
        "avg_utilisation": round(sum(peaks.values()) / len(peaks), 4) if peaks else 0.0,
        "transport_pressure": mean_peak({"transport_node", "transport_route"}),
        "road_pressure": mean_peak({"road"}),
        "venue_pressure": mean_peak({"venue", "gate"}),
        "hotel_pressure": round(sum(hotels) / len(hotels), 4) if hotels else 0.0,
        "queued_people": float(stats["queued_people"]),
        "late_entries": float(stats["late_entries"]),
        "rooms_unmet": float(stats["rooms_unmet"]),
        "unmet_demand": round(float(stats["late_entries"]) + float(stats["rooms_unmet"]) * 2.2
                              + float(stats["unparked_people"]), 1),
        "avg_travel_time_sec": float(stats["avg_travel_time_sec"]),
    }


class ProjectionService:
    """Cached look-ahead of the live city under its announced schedule."""

    def __init__(self) -> None:
        self._cache: dict[str, Any] | None = None
        self._key: tuple | None = None

    def invalidate(self) -> None:
        self._cache = None
        self._key = None

    def get(self, engine: Any) -> dict[str, Any]:
        cfg = get_config().raw.get("projection", {})
        refresh = max(1, int(cfg.get("refresh_cycles", 4)))
        key = (engine.store.cycle_number // refresh, engine.world_version)
        if self._cache is not None and self._key == key:
            return self._cache
        horizon = float(cfg.get("horizon_sec", 5400))
        # Long enough to cover the latest event's egress, so a return trip is
        # planned against the post-event crush rather than today's calm.
        end_offsets = []
        for ev in engine.events.events.values():
            if ev["status"] == "cancelled":
                continue
            end_offsets.append((parse(ev["end_time"]) - parse(engine.store.sim_time)).total_seconds() + 3600)
        horizon = min(max([horizon] + end_offsets), float(cfg.get("max_horizon_sec", 6 * 3600)))
        with engine.world_lock:
            clone = engine.generator.clone()
        result = run_forward(clone, horizon, int(cfg.get("step_sec", 120)), int(cfg.get("sample_sec", 300)),
                             critical_map(engine))
        result.pop("gen", None)
        result["sim_time"] = engine.store.sim_time
        result["horizon_sec"] = horizon
        self._cache, self._key = result, key
        return result

    @staticmethod
    def at(projection: dict[str, Any], offset_sec: float) -> dict[str, Any]:
        """The sample nearest to `offset_sec` (clamped to the horizon)."""
        samples = projection["samples"]
        return min(samples, key=lambda s: abs(s["offset_sec"] - offset_sec))


PROJECTIONS = ProjectionService()
