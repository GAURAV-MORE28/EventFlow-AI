"""What-if simulation (01_BACKEND_CONTRACT.md §3.8).

Phase 1B rewrite. "What if X happened now?" means:

    same current world  +  same event-world model  +  same horizon  +  only X changed

Both branches are CLONES of the live `SyntheticGenerator` (the event-world
model the live simulation runs on), taken at the same instant:

    baseline branch  = clone, nothing applied            (do nothing)
    scenario branch  = clone + X applied ONCE, at "now"  (e.g. demand x 0.8 means
                                                           x 0.8, not x 0.8 per step)

and both are evaluated at the same times, one per 30-sim-second cycle, up to
`horizon_sec`. Cloning never touches the live generator, the live twin, or any
RNG (the generator has none; the twin is not used here any more). The previous
implementation rolled a twin-surrogate branch whose own dynamics exploded
(baseline peaks of 2,048-8,055%, audit P0-02).

Definitions (one side = one branch):
    peak_utilisation   max utilisation over all entities and all horizon steps
    peak_entity_id     where that peak occurs
    critical_count     entities that reach >= 0.90 at any step in the horizon
    load_variance      population variance of ZONE utilisation at the horizon
                       end (the same definition as the live `load_variance`)
    new_critical_entities  reach >= 0.90 in the scenario but never in baseline

In this synthetic environment the event-world model IS the generator, so a
what-if is an exact projection of the simulation — not a forecast of real crowds.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..errors import ApiError
from ..ml_reference.common import stable_unit, variance
from ..simtime import shift

log = logging.getLogger("eventflow.simulate")

CYCLE_SEC = 30

# Which entity types a targeted scenario may name. Anything else is rejected —
# `gate_99` or a gate closure aimed at a road must not "succeed" with 0% effect.
TARGET_TYPES: dict[str, set[str]] = {
    "metro_capacity_delta": {"transport_node", "transport_route"},
    "road_capacity_delta": {"road"},
    "parking_loss": {"parking"},
    "gate_closure": {"gate"},
    "transport_outage": {"transport_node", "transport_route"},
}
DELTA_SCENARIOS = {"attendance_delta", "metro_capacity_delta", "road_capacity_delta", "parking_loss"}
RAIN_INTENSITIES = {"light", "moderate", "heavy"}


def validate_scenarios(scenarios: list[dict], nodes: dict[str, dict]) -> None:
    """Raise INVALID_SCENARIO for anything the world model cannot apply honestly."""
    for s in scenarios:
        stype = s["scenario_type"]
        params = s.get("params") or {}
        if stype in TARGET_TYPES:
            eid = params.get("entity_id")
            if not eid:
                raise ApiError("INVALID_SCENARIO", f"'{stype}' requires params.entity_id.", {"scenario_type": stype})
            if eid not in nodes:
                raise ApiError("INVALID_SCENARIO", f"Unknown entity '{eid}' in '{stype}'.",
                               {"scenario_type": stype, "entity_id": eid})
            if nodes[eid]["entity_type"] not in TARGET_TYPES[stype]:
                raise ApiError(
                    "INVALID_SCENARIO",
                    f"'{stype}' cannot target a {nodes[eid]['entity_type']} ('{eid}').",
                    {"scenario_type": stype, "entity_id": eid},
                )
        if stype in DELTA_SCENARIOS:
            delta = params.get("delta_pct")
            if not isinstance(delta, (int, float)) or isinstance(delta, bool):
                raise ApiError("INVALID_SCENARIO", f"'{stype}' requires numeric params.delta_pct.", {"scenario_type": stype})
            if not -100.0 < float(delta) <= 500.0:
                raise ApiError("INVALID_SCENARIO", f"delta_pct must be in (-100, 500], got {delta}.",
                               {"scenario_type": stype, "delta_pct": delta})
        if stype == "weather_rain" and str(params.get("intensity", "moderate")) not in RAIN_INTENSITIES:
            raise ApiError("INVALID_SCENARIO", "weather_rain intensity must be light, moderate or heavy.",
                           {"intensity": params.get("intensity")})
        if stype == "concurrent_event":
            overlap = params.get("overlap_pct", 15.0)
            if not isinstance(overlap, (int, float)) or not 0.0 <= float(overlap) <= 500.0:
                raise ApiError("INVALID_SCENARIO", "overlap_pct must be in [0, 500].", {"overlap_pct": overlap})
        if stype == "combined":
            subs = params.get("scenarios")
            if not isinstance(subs, list) or not subs:
                raise ApiError("INVALID_SCENARIO", "'combined' requires a non-empty params.scenarios list.")
            for sub in subs:
                if not isinstance(sub, dict) or "scenario_type" not in sub:
                    raise ApiError("INVALID_SCENARIO", "Each combined sub-scenario needs a scenario_type.")
            validate_scenarios(subs, nodes)


def _side(frames: list[dict[str, dict]], zone_ids: list[str]) -> tuple[dict, set[str]]:
    peak, peak_entity = 0.0, None
    reached: set[str] = set()
    for frame in frames:
        for eid, v in frame.items():
            u = v["utilisation"]
            if u > peak:
                peak, peak_entity = u, eid
            if u >= 0.90:
                reached.add(eid)
    last = frames[-1]
    return {
        "peak_utilisation": round(peak, 4),
        "peak_entity_id": peak_entity,
        "load_variance": round(variance([last[z]["utilisation"] for z in zone_ids if z in last]), 4),
        "critical_count": len(reached),
    }, reached


def roll(world: Any, horizon_sec: int, exclude: tuple[str, ...] = ()) -> list[dict[str, dict]]:
    """Evaluate a (cloned) world at every cycle boundary over the horizon."""
    start = world.elapsed_sec()
    steps = max(1, int(horizon_sec) // CYCLE_SEC)
    return [world.evaluate(start + CYCLE_SEC * (k + 1), exclude=exclude) for k in range(steps)]


class SimulationRegistry:
    def __init__(self) -> None:
        self._jobs: dict[str, dict[str, Any]] = {}
        self._counter = 0

    def new_id(self) -> str:
        self._counter += 1
        return "sim_" + f"{int(stable_unit('sim', self._counter) * 0xFFFF):04x}"

    def get(self, simulation_id: str) -> dict | None:
        return self._jobs.get(simulation_id)

    def create(self, simulation_id: str, label: str | None) -> dict:
        job = {
            "simulation_id": simulation_id,
            "status": "running",
            "label": label,
            "baseline": None,
            "scenario": None,
            "delta": None,
            "cascade": None,
            "candidate_interventions": [],
        }
        self._jobs[simulation_id] = job
        return job

    async def run(self, engine: Any, simulation_id: str, scenarios: list[dict], horizon_sec: int) -> None:
        try:
            result = await asyncio.to_thread(self._execute, engine, scenarios, horizon_sec)
            job = self._jobs[simulation_id]
            job.update(result)
            job["status"] = "complete"
        except Exception:
            log.exception("simulation %s failed", simulation_id)
            self._jobs[simulation_id]["status"] = "failed"

    def run_sync(self, engine: Any, scenarios: list[dict], horizon_sec: int, label: str | None) -> dict:
        """Used by the Commander's `run_whatif` tool, which needs a value now."""
        validate_scenarios(scenarios, engine.store.nodes)
        return self._execute(engine, scenarios, horizon_sec)

    def _execute(self, engine: Any, scenarios: list[dict], horizon_sec: int) -> dict:
        store = engine.store
        node_state = store.node_state_for_ml()
        zone_ids = store.zone_ids()

        # Same instant, same model, two independent clones.
        baseline_world = engine.generator.clone()
        scenario_world = engine.generator.clone()
        for s in scenarios:
            scenario_world.inject(s["scenario_type"], s.get("params") or {})

        base_frames = roll(baseline_world, horizon_sec)
        scen_frames = roll(scenario_world, horizon_sec)
        baseline, base_reached = _side(base_frames, zone_ids)
        scenario_side, scen_reached = _side(scen_frames, zone_ids)

        def pct(new: float, old: float) -> float:
            if not old:
                return 0.0
            return round((new - old) / old * 100.0, 1)

        new_critical = sorted(scen_reached - base_reached)
        delta = {
            "peak_utilisation_pct": pct(scenario_side["peak_utilisation"], baseline["peak_utilisation"]),
            "load_variance_pct": pct(scenario_side["load_variance"], baseline["load_variance"]),
            "new_critical_entities": new_critical,
        }

        # Cascade and candidate actions for the worst entity under the scenario,
        # with the scenario's own projection standing in for the forecast.
        worst_id = scenario_side.get("peak_entity_id")
        idx_1800 = min(len(scen_frames), 1800 // CYCLE_SEC) - 1
        cascade = None
        candidates: list[dict] = []
        if worst_id:
            scenario_state = dict(node_state)
            for eid in scenario_state:
                projected = scen_frames[idx_1800][eid]["utilisation"]
                scenario_state[eid] = {**scenario_state[eid], "forecast_1800": projected}
            cascade = engine.registry.cascade.predict(
                worst_id, scenario_state, store.edges, generated_at=store.sim_time
            )
            raw = engine.registry.optimiser.generate(
                {
                    "root_entity_id": worst_id,
                    "cascade": cascade,
                    "node_state": scenario_state,
                    "edges": store.edges,
                    "sim_time": store.sim_time,
                },
                3,
            )
            for c in raw:
                c["certificate"] = engine.certify_candidate(c, scenario_state, world=scenario_world)
                c["created_at"] = store.sim_time
                c["expires_at"] = shift(store.sim_time, c.pop("_ttl_sec", 900))
            candidates = engine.registry.optimiser.rank(raw)

        return {
            "baseline": baseline,
            "scenario": scenario_side,
            "delta": delta,
            "cascade": cascade,
            "candidate_interventions": candidates,
            # Internal (stripped before the wire): per-entity trajectories, for tests/diagnostics.
            "_trajectories": {
                "baseline": {e: [f[e]["utilisation"] for f in base_frames] for e in store.nodes},
                "scenario": {e: [f[e]["utilisation"] for f in scen_frames] for e in store.nodes},
                "start_elapsed_sec": baseline_world.elapsed_sec(),
            },
        }


SIMULATIONS = SimulationRegistry()
