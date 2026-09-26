"""What-if simulation (01_BACKEND_CONTRACT.md §3.8).

`POST /simulate` returns 202 immediately and the job runs in the background:

    live city ──clone──► baseline copy ──run N steps──┐
              └─clone──► scenario copy + change ──run──┴─► compare

Both copies are clones of the live SyntheticGenerator, so the what-if uses the
same demand, routing, queueing and hotel physics as the running city, and it can
never contaminate it. Results are peaks over the horizon (not end states), so a
short spike is not hidden by a calm finish.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..ml_reference.common import stable_unit
from ..simtime import shift
from .evaluation import evaluate_candidates
from .projection import critical_map, run_forward, summarise_side

log = logging.getLogger("eventflow.simulate")

COMPARED = (
    "peak_utilisation", "load_variance", "avg_utilisation", "transport_pressure", "road_pressure",
    "venue_pressure", "hotel_pressure", "queued_people", "late_entries", "rooms_unmet",
    "unmet_demand", "avg_travel_time_sec",
)


def _pct(new: float, old: float) -> float:
    if not old:
        return 0.0 if not new else 100.0
    return round((new - old) / old * 100.0, 1)


class SimulationRegistry:
    def __init__(self) -> None:
        self._jobs: dict[str, dict[str, Any]] = {}
        self._counter = 0

    def new_id(self) -> str:
        self._counter += 1
        return "sim_" + f"{int(stable_unit('sim', self._counter) * 0xFFFF):04x}"

    def clear(self) -> None:
        """A new world was activated: results computed on the old graph are meaningless."""
        self._jobs.clear()

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
        # Bounded memory: keep the most recent 50 jobs.
        while len(self._jobs) > 50:
            self._jobs.pop(next(iter(self._jobs)))
        return job

    async def run(self, engine: Any, simulation_id: str, scenarios: list[dict], horizon_sec: int) -> None:
        try:
            result = await asyncio.to_thread(self._execute, engine, scenarios, horizon_sec)
            job = self._jobs[simulation_id]
            job.update(result)
            job["status"] = "complete"
        except Exception:
            log.exception("simulation %s failed", simulation_id)
            if simulation_id in self._jobs:
                self._jobs[simulation_id]["status"] = "failed"

    def run_sync(self, engine: Any, scenarios: list[dict], horizon_sec: int, label: str | None) -> dict:
        """Used by the Commander's `run_whatif` tool, which needs a value now."""
        return self._execute(engine, scenarios, horizon_sec, with_candidates=False)

    def _execute(self, engine: Any, scenarios: list[dict], horizon_sec: int, with_candidates: bool = True) -> dict:
        store = engine.store
        critical = critical_map(engine)
        step = 60
        with engine.world_lock:
            base_gen = engine.generator.clone()
            scen_gen = engine.generator.clone()
        for s in scenarios:
            scen_gen.inject(s["scenario_type"], s.get("params", {}), source="whatif")
        scen_start = scen_gen.clone()  # the scenario at t0, for candidate evaluation

        base = run_forward(base_gen, horizon_sec, step, 300, critical)
        scen = run_forward(scen_gen, horizon_sec, step, 300, critical)
        types = base_gen.types
        b_side = summarise_side(base, types)
        s_side = summarise_side(scen, types)

        new_critical = sorted(scen["critical"] - base["critical"])
        delta = {
            "peak_utilisation_pct": _pct(s_side["peak_utilisation"], b_side["peak_utilisation"]),
            "load_variance_pct": _pct(s_side["load_variance"], b_side["load_variance"]),
            "new_critical_entities": new_critical,
            "resolved_critical_entities": sorted(base["critical"] - scen["critical"]),
            "metrics": {k: round(s_side[k] - b_side[k], 4) for k in COMPARED},
        }

        # Biggest movers, so the operator sees *where* the scenario bites.
        changes = []
        for e in types:
            if types[e] in ("hotel",):
                continue
            b, a = base["peaks"].get(e, 0.0), scen["peaks"].get(e, 0.0)
            if abs(a - b) >= 0.02:
                changes.append({"entity_id": e, "display_name": store.nodes[e]["display_name"],
                                "entity_type": types[e], "baseline_peak": round(b, 4), "scenario_peak": round(a, 4)})
        changes.sort(key=lambda c: -abs(c["scenario_peak"] - c["baseline_peak"]))

        timeline = []
        for bs, ss in zip(base["samples"], scen["samples"]):
            def peak(sample: dict) -> float:
                vals = [u for e, u in sample["util"].items() if types[e] != "hotel"]
                return round(max(vals), 4) if vals else 0.0
            timeline.append({"offset_sec": bs["offset_sec"], "baseline_peak": peak(bs), "scenario_peak": peak(ss)})

        cascade = None
        candidates: list[dict] = []
        worst = new_critical[0] if new_critical else s_side["peak_entity_id"]
        if worst and with_candidates:
            # The cascade and candidate actions are computed on the scenario's
            # own worst moment, with the engine's registry (same models as live).
            end_state = {}
            peak_util = scen["peaks"]
            base_state = store.node_state_for_ml()
            for eid, st in base_state.items():
                u = float(scen["final"].get(eid, st["utilisation"]))
                end_state[eid] = {**st, "utilisation": u, "forecast_1800": float(peak_util.get(eid, u))}
            risk = engine.registry.risk.score(end_state, {}, {})
            for eid, r in risk.items():
                end_state[eid]["risk_score"] = r["risk_score"]
                end_state[eid]["risk_band"] = r["risk_band"]
            from .cascade_flow import cascade_for

            closed = set(scen_start.closed_entities()) if hasattr(scen_start, "closed_entities") else set()
            cascade = cascade_for(worst, end_state, store.edges, engine.config.thresholds_for,
                                  engine.config.raw.get("cascade", {}), store.sim_time, closed)
            raw = engine.registry.optimiser.generate(
                {"root_entity_id": worst, "cascade": cascade, "node_state": end_state, "edges": store.edges,
                 "sim_time": store.sim_time, **engine.optimiser_context()},
                3,
            )
            # Evaluate each candidate inside the scenario world, not the live one.
            holder = type("ScenarioEngine", (), {})()
            holder.config = engine.config
            holder.world_lock = engine.world_lock
            holder.generator = scen_start
            holder.critical_lines = critical
            evaluate_candidates(holder, raw, worst, engine.current_compliance())
            min_relief = float(engine.icfg.get("min_relief_pct", 1.0))
            raw = [c for c in raw if c.get("evaluation") is None or c["estimated_relief_pct"] >= min_relief]
            for c in raw:
                c["certificate"] = engine.registry.equilibrium.certify(c, end_state, store.edges, store.segments)
                c["created_at"] = store.sim_time
                c["expires_at"] = shift(store.sim_time, c.pop("_ttl_sec", 900))
            candidates = engine.registry.optimiser.rank(raw)

        return {
            "baseline": b_side,
            "scenario": s_side,
            "delta": delta,
            "cascade": cascade,
            "candidate_interventions": candidates,
            "top_changes": changes[:10],
            "timeline": timeline,
            "horizon_sec": int(horizon_sec),
            "scenarios": scenarios,
        }


SIMULATIONS = SimulationRegistry()
