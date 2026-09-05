"""What-if simulation (01_BACKEND_CONTRACT.md §3.8).

`POST /simulate` returns 202 immediately and the job runs in the background over
a *branch* of the twin — never the live ensemble, so a what-if can never
contaminate the running demo.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..ml_reference.common import stable_unit
from ..simtime import shift

log = logging.getLogger("eventflow.simulate")

# Scenario -> the capacity/demand multipliers the twin branch understands.
def scenario_to_multipliers(scenario: dict, node_state: dict[str, dict]) -> tuple[dict, dict]:
    stype = scenario["scenario_type"]
    params = scenario.get("params", {})
    cap: dict[str, float] = {}
    demand: dict[str, float] = {}
    eid = params.get("entity_id")
    delta = float(params.get("delta_pct", 0.0)) / 100.0

    if stype == "attendance_delta":
        for e in node_state:
            demand[e] = 1.0 + delta
    elif stype in ("metro_capacity_delta", "road_capacity_delta", "parking_loss"):
        if eid:
            cap[eid] = 1.0 + delta
    elif stype == "weather_rain":
        factor = {"light": 1.05, "moderate": 1.12, "heavy": 1.22}.get(str(params.get("intensity", "moderate")), 1.12)
        for e, s in node_state.items():
            if s.get("entity_type") in ("road", "parking", "gate"):
                demand[e] = factor
    elif stype in ("gate_closure", "transport_outage"):
        if eid:
            cap[eid] = 0.01
            same_type = [
                e for e, s in node_state.items()
                if s.get("entity_type") == node_state.get(eid, {}).get("entity_type") and e != eid
            ]
            for e in same_type:
                demand[e] = 1.0 + 1.0 / max(len(same_type), 1)
    elif stype == "hotel_shortage":
        for e, s in node_state.items():
            if s.get("entity_type") == "hotel":
                demand[e] = 1.15
    elif stype == "concurrent_event":
        for e in node_state:
            demand[e] = 1.0 + float(params.get("overlap_pct", 15.0)) / 100.0
    elif stype == "combined":
        for sub in params.get("scenarios", []):
            c, d = scenario_to_multipliers(sub, node_state)
            cap.update(c)
            demand.update(d)
    return cap, demand


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
        return self._execute(engine, scenarios, horizon_sec)

    def _execute(self, engine: Any, scenarios: list[dict], horizon_sec: int) -> dict:
        store = engine.store
        node_state = store.node_state_for_ml()

        cap: dict[str, float] = {}
        demand: dict[str, float] = {}
        for s in scenarios:
            c, d = scenario_to_multipliers(s, node_state)
            cap.update(c)
            demand.update(d)

        branch = engine.registry.twin.branch(
            {"capacity_multipliers": cap, "demand_multipliers": demand}, horizon_sec
        )
        baseline = branch["baseline"]
        scenario_side = branch["scenario"]

        def pct(new: float, old: float) -> float:
            if old in (0, None):
                return 0.0
            return round((new - old) / old * 100.0, 1)

        delta = {
            "peak_utilisation_pct": pct(scenario_side["peak_utilisation"], baseline["peak_utilisation"]),
            "load_variance_pct": pct(scenario_side["load_variance"], baseline["load_variance"]),
            "new_critical_entities": branch["new_critical_entities"],
        }

        # Cascade and candidate actions for the worst entity under the scenario.
        worst = scenario_side.get("peak_entity_id") or branch["new_critical_entities"][:1]
        worst_id = worst if isinstance(worst, str) else (worst[0] if worst else None)

        cascade = None
        candidates: list[dict] = []
        if worst_id:
            scenario_state = dict(node_state)
            for eid, traj in branch["trajectory"].items():
                if eid in scenario_state and traj:
                    scenario_state[eid] = {**scenario_state[eid], "forecast_1800": traj[-1]}
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
                c["certificate"] = engine.registry.equilibrium.certify(
                    c, scenario_state, store.edges, store.segments
                )
                c["created_at"] = store.sim_time
                c["expires_at"] = shift(store.sim_time, c.pop("_ttl_sec", 900))
            candidates = engine.registry.optimiser.rank(raw)

        return {
            "baseline": baseline,
            "scenario": scenario_side,
            "delta": delta,
            "cascade": cascade,
            "candidate_interventions": candidates,
        }


SIMULATIONS = SimulationRegistry()
