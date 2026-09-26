"""The orchestration cycle (01_BACKEND_CONTRACT.md §2).

    1. generator.tick()                     -> observations (live city, flow model)
       nominal.tick()                       -> the twin's process model (announced plan only)
       counterfactual.tick()                -> do-nothing worlds for approved interventions
    2. persist observations                 -> DB + cache
    3. twin.step(model) + assimilate(obs)   -> corrected ensemble
    4. forecaster.predict(entities)         -> Forecast[]
    5. risk_scorer.score(state, forecast)   -> risk_score per entity
    6. anomaly.detect(residuals)            -> anomaly flags
    7. cascade.predict(graph_state)         -> CascadeResult[]
    8. IF any entity predicted critical within 3600s:
         optimise -> simulate each candidate (evaluation) -> certify -> rank -> queue
    9. broadcast WS events
   10. write cycle metrics

Backpressure rule (§2): skip the forecast refresh before you skip assimilation.

Three worlds run side by side, all SyntheticGenerator instances:
  * `generator`       — the city as it really is (disruptions, interventions, schedule)
  * `nominal`         — the city as planned: announced schedule and approved
                        interventions, but not unannounced disruptions. It is the
                        twin's process model; assimilation corrects the gap.
  * `counterfactuals` — per approved intervention, the live city forked just
                        before approval without it. Realised relief is measured
                        against it, not guessed.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from collections import deque
from typing import Any

from ..cache import CACHE, TTL
from ..config import get_config
from ..ml_reference.common import band_from_score, clamp, variance
from ..ml_registry import MLRegistry, call_ml
from ..simtime import iso, parse, shift
from ..ws.manager import MANAGER
from .events import EventSchedule
from .state_store import StateStore

log = logging.getLogger("eventflow.cycle")

# 01 §3.4 — the pressure timeline is always these six offsets.
TRAJECTORY_OFFSETS = [0, 300, 600, 900, 1200, 1800]
MIN_WALL_SLEEP = 0.2
ROOT_COOLDOWN_CYCLES = 10
PRIOR_COMPLIANCE_WEIGHT = 10.0


class Engine:
    """Owns the clock, the ML registry, the simulated worlds, and the only writer to StateStore."""

    def __init__(self) -> None:
        self.config = get_config()
        raw = self.config.raw
        event_cfg = raw["event"]

        self.seed = self.config.demo_seed
        self.speed_multiplier = float(raw.get("speed_multiplier", 10))
        self.sim_dt = self.config.cycle_sec
        self.paused = False
        self.icfg = raw.get("interventions", {})

        self.store = StateStore(sim_start=event_cfg["sim_start_time"])
        from ..providers.data import get_data_provider

        self.events = EventSchedule(
            get_data_provider().events(raw.get("events") or [self._primary_from_event_cfg(event_cfg)]),
            event_cfg["event_id"],
        )
        # Guards every generator mutation and every clone, so a what-if or a
        # projection never copies a half-applied cycle.
        self.world_lock = threading.RLock()
        self.world_version = 0
        self.counterfactuals: dict[str, Any] = {}
        self._build_worlds()

        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self._last_cycle_ms = 0.0
        self._shed_forecast_until = 0
        self.commander = None
        self.critical_lines = {e: self.config.thresholds_for(n["entity_type"])[1] for e, n in self.store.nodes.items()}

    @staticmethod
    def _primary_from_event_cfg(cfg: dict) -> dict:
        return {
            "event_id": cfg["event_id"], "name": cfg["name"], "venue_entity_id": cfg["venue_entity_id"],
            "start_time": cfg["start_time"], "end_time": cfg["end_time"],
            "expected_attendance": cfg["expected_attendance"],
        }

    # --- worlds ----------------------------------------------------------------
    def _build_worlds(self) -> None:
        self.registry = MLRegistry()
        topology = {
            "nodes": list(self.store.nodes.values()),
            "edges": self.store.edges,
            "properties": self.store.properties,
            "events": self.events.to_generator(),
        }
        self.generator = self.registry.build_generator(topology, self.seed)
        if hasattr(self.generator, "clone"):
            self.nominal = self.generator.clone(sources={"intervention", "schedule"})
        else:  # an ML drop-in generator without clone(): the twin runs model-free
            self.nominal = None
        self.counterfactuals = {}
        self._init_twin()
        self.world_version += 1

    def _capacity(self, eid: str) -> float:
        if hasattr(self.generator, "capacity"):
            return float(self.generator.capacity(eid))
        return float(self.store.nodes[eid]["nominal_capacity"])

    def _init_twin(self) -> None:
        caps = {e: self._capacity(e) for e in self.store.nodes}
        truth = self.generator.ground_truth()
        counts = {e: v["current_count"] for e, v in truth.items()}
        if hasattr(self.registry.twin, "initialise"):
            self.registry.twin.initialise(list(self.store.nodes.keys()), caps, counts)

    def _all_worlds(self, include_nominal: bool = True) -> list[Any]:
        worlds = [self.generator] + list(self.counterfactuals.values())
        if include_nominal and self.nominal is not None:
            worlds.append(self.nominal)
        return worlds

    def world_changed(self) -> None:
        self.world_version += 1
        from .projection import PROJECTIONS

        PROJECTIONS.invalidate()

    # --- lifecycle ------------------------------------------------------------
    async def start(self) -> None:
        self._stopping.clear()
        self._task = asyncio.create_task(self._loop(), name="eventflow-cycle")
        log.info(
            "cycle started: seed=%s dt=%ss speed=%sx modules=%s",
            self.seed, self.sim_dt, self.speed_multiplier, self.registry.provenance,
        )

    async def stop(self) -> None:
        self._stopping.set()
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._task = None

    async def _loop(self) -> None:
        while not self._stopping.is_set():
            wall_sleep = max(MIN_WALL_SLEEP, self.sim_dt / max(self.speed_multiplier, 1e-6))
            if self.paused:
                await asyncio.sleep(wall_sleep)
                continue
            try:
                started = time.perf_counter()
                await self.run_cycle()
                elapsed = time.perf_counter() - started
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("cycle failed; continuing to the next one")
                elapsed = 0.0
            await asyncio.sleep(max(0.0, wall_sleep - elapsed))

    # --- the cycle -------------------------------------------------------------
    def _tick_worlds(self) -> tuple[dict, dict, dict | None]:
        with self.world_lock:
            model = None
            if self.nominal is not None:
                before = {e: v["current_count"] for e, v in self.nominal.ground_truth().items()}
                self.nominal.tick(self.sim_dt)
                after = {e: v["current_count"] for e, v in self.nominal.ground_truth().items()}
                model = {"counts": after, "delta": {e: after[e] - before.get(e, after[e]) for e in after}}
            observations = self.generator.tick(self.sim_dt)
            truth = self.generator.ground_truth()
            for cf in self.counterfactuals.values():
                cf.tick(self.sim_dt)
        return observations, truth, model

    async def run_cycle(self) -> None:
        cycle_started = time.perf_counter()
        store = self.store
        previous_states = store.snapshot_states()
        store.advance(self.sim_dt)
        sim_time = store.sim_time

        # 1. observations ------------------------------------------------------
        observations, truth, model = await asyncio.to_thread(self._tick_worlds)

        # 3. assimilate — never skipped (§2 latency table) ----------------------
        if hasattr(self.registry.twin, "step"):
            try:
                await asyncio.to_thread(self.registry.twin.step, self.sim_dt, model)
            except TypeError:
                await asyncio.to_thread(self.registry.twin.step, self.sim_dt)
            except Exception:
                log.exception("twin.step failed; assimilation will run against a stale ensemble")

        fidelity, twin_degraded, assimilate_ms = await call_ml(
            "twin.assimilate",
            self.registry.twin.assimilate,
            self.registry.twin.fallback,
            self.config.budget_sec("assimilate") * 4,
            observations,
            sim_time,
            {e: v["current_count"] for e, v in truth.items()},
        )
        if assimilate_ms > self.config.budget_ms("assimilate"):
            log.warning("assimilate took %.0fms (budget %dms)", assimilate_ms, self.config.budget_ms("assimilate"))

        twin_state = self.registry.twin.state() if hasattr(self.registry.twin, "state") else {}
        self._merge_states(observations, twin_state, truth, sim_time)

        # 4. forecast ------------------------------------------------------------
        if store.cycle_number >= self._shed_forecast_until:
            prior = await asyncio.to_thread(self._model_prior)
            forecasts, forecast_degraded, forecast_ms = await call_ml(
                "forecaster.predict",
                self._predict_with_prior,
                self.registry.forecaster.fallback,
                self.config.budget_sec("forecast"),
                store.series(),
                store.capacities(),
                self.config.horizons_sec,
                sim_time,
                prior,
            )
            self._apply_forecasts(forecasts or {}, sim_time)
        else:
            log.info("shedding forecast refresh this cycle (backpressure)")

        # 5. risk ------------------------------------------------------------------
        node_state = store.node_state_for_ml()
        exposure = self._cascade_exposure()
        scores, _, _ = await call_ml(
            "risk.score", self.registry.risk.score, self.registry.risk.fallback, 0.2,
            node_state, store.forecasts, exposure,
        )
        self._apply_risk(scores or {})

        # 6. anomalies --------------------------------------------------------------
        anomalies, _, _ = await call_ml(
            "anomaly.detect", self.registry.anomaly.detect, self.registry.anomaly.fallback, 0.15,
            {e: list(r) for e, r in store.residuals.items()},
        )

        # 7. cascades ----------------------------------------------------------------
        node_state = store.node_state_for_ml()
        cascades, cascade_degraded, cascade_ms = await call_ml(
            "cascade.predict_all",
            self.registry.cascade.predict_all,
            None,
            self.config.budget_sec("cascade"),
            node_state,
            store.edges,
            sim_time,
        )
        if cascades is None:
            cascades = [
                self.registry.cascade.fallback(root, node_state, store.edges, None, sim_time)
                for root in self._critical_roots(node_state)
            ]
        new_cascades = self._apply_cascades(cascades)

        # Re-score risk now that cascade exposure is known, so `cascading` is real.
        exposure = self._cascade_exposure()
        scores, _, _ = await call_ml(
            "risk.score", self.registry.risk.score, self.registry.risk.fallback,
            0.2, node_state, store.forecasts, exposure,
        )
        self._apply_risk(scores or {})
        self._recompute_summary()
        self._resolve_cascade_predictions()
        self._track_cascade_recall(previous_states)

        # 8. interventions -------------------------------------------------------------
        node_state = store.node_state_for_ml()
        new_interventions = await self._maybe_generate_interventions(node_state, sim_time)
        expired = store.expire_interventions()
        for i in expired:
            store.root_cooldown[i.get("triggered_by_entity_id") or ""] = store.cycle_number
        settled = await self._settle_executing_interventions(sim_time)

        with self.world_lock:
            store.operations = self.generator.stats() if hasattr(self.generator, "stats") else {}

        # 2. persist ------------------------------------------------------------------
        await asyncio.to_thread(self._persist, sim_time)
        self._write_cache()

        # 9. broadcast -------------------------------------------------------------------
        await self._broadcast(previous_states, fidelity, anomalies or [], new_cascades,
                              new_interventions, expired, settled, sim_time)

        # 10. cycle metrics ----------------------------------------------------------------
        total_ms = (time.perf_counter() - cycle_started) * 1000.0
        self._last_cycle_ms = total_ms
        store.cycle_latency_ms.append(total_ms)
        self._maybe_shed_load(total_ms)

    # --- step 3/1: merge observations and twin estimates -------------------------------
    def _merge_states(
        self,
        observations: dict[str, float],
        twin_state: dict[str, dict],
        truth: dict[str, dict],
        sim_time: str,
    ) -> None:
        """Observed entities report their reading; the rest (and sensors that
        missed this cycle) take the twin's estimate — the last valid state
        carried forward by the process model, never an unconstrained guess."""
        store = self.store
        for eid in store.nodes:
            critical = self.config.thresholds_for(store.nodes[eid]["entity_type"])[1]
            cap = self._capacity(eid) or 1.0
            observed = eid in observations

            if observed:
                count = float(observations[eid])
            elif eid in twin_state:
                count = float(twin_state[eid]["current_count"])
            else:
                count = float(truth.get(eid, {}).get("current_count", 0.0))

            previous = store.entity_states.get(eid)
            prev_count = float(previous["current_count"]) if previous else count
            utilisation = round(clamp(count / cap, 0.0, 2.0), 4)
            store.cycles_over_critical[eid] = store.cycles_over_critical.get(eid, 0) + 1 if utilisation >= critical else 0

            tw = twin_state.get(eid) or {}
            store.twin_layers[eid] = {
                "observed_utilisation": round(float(observations[eid]) / cap, 4) if observed else None,
                "estimated_utilisation": round(float(tw["current_count"]) / cap, 4) if "current_count" in tw else None,
                "estimate_std": round(float(tw["ensemble_std"]) / cap, 4) if tw.get("ensemble_std") is not None else None,
            }
            store.entity_states[eid] = {
                "entity_id": eid,
                "sim_time": sim_time,
                "current_count": round(count, 1),
                "utilisation": utilisation,
                "flow_rate_per_min": round((count - prev_count) * (60.0 / self.sim_dt), 1),
                "risk_score": int(previous["risk_score"]) if previous else 0,
                "risk_band": previous["risk_band"] if previous else "low",
                "is_observed": observed,
            }
            store.history[eid].append(utilisation)

    # --- step 4 -------------------------------------------------------------------------
    VALIDATION_HORIZON_SEC = 900

    def _predict_with_prior(self, series, capacities, horizons, sim_time, prior):
        """Forecaster call that passes the twin's model projection when the
        forecaster supports it (an ML drop-in without `model_prior` still works)."""
        try:
            return self.registry.forecaster.predict(series, capacities, horizons, sim_time, model_prior=prior)
        except TypeError:
            return self.registry.forecaster.predict(series, capacities, horizons, sim_time)

    def _model_prior(self) -> dict[str, dict] | None:
        """Projection of the twin's process model (announced plan + approved
        actions, not unannounced disruptions) at the forecast horizons.
        Refreshed every `forecaster.model_refresh_cycles` and whenever the plan
        changes; cheap in between."""
        if self.nominal is None or not hasattr(self.nominal, "clone"):
            return None
        refresh = int(self.config.raw.get("forecaster", {}).get("model_refresh_cycles", 4))
        key = (self.store.cycle_number // max(refresh, 1), self.world_version)
        cached = getattr(self, "_prior_cache", None)
        if cached and cached[0] == key:
            return cached[1]
        horizons = sorted(self.config.horizons_sec)
        with self.world_lock:
            clone = self.nominal.clone()
        now = clone.utilisation()
        points: dict[str, dict[int, float]] = {e: {} for e in now}
        elapsed, step = 0, 60
        for h in horizons:
            while elapsed < h:
                clone.tick(step)
                elapsed += step
            for e, u in clone.utilisation().items():
                points[e][h] = u
        prior = {e: {"now": now[e], "points": points[e]} for e in now}
        self._prior_cache = (key, prior)
        return prior

    def _apply_forecasts(self, forecasts: dict[str, dict], sim_time: str) -> None:
        store = self.store
        sources: dict[str, int] = {}
        lag_cycles = max(1, self.VALIDATION_HORIZON_SEC // self.sim_dt)

        for eid, raw in forecasts.items():
            if eid not in store.nodes:
                continue
            actual = store.entity_states[eid]["utilisation"]
            snapshots = store.forecast_snapshots[eid]
            due_cycle = store.cycle_number - lag_cycles
            while snapshots and snapshots[0][0] < due_cycle:
                snapshots.popleft()
            if snapshots and snapshots[0][0] == due_cycle:
                _, predicted_900, baseline_then = snapshots.popleft()
                residual = actual - predicted_900
                store.residuals[eid].append(residual)
                store.forecast_errors["model"].append(abs(residual))
                store.forecast_errors["persistence"].append(abs(actual - baseline_then))

            forecast = dict(raw)
            forecast["entity_id"] = eid
            forecast["generated_at"] = sim_time
            store.forecasts[eid] = forecast
            sources[forecast["source"]] = sources.get(forecast["source"], 0) + 1

            predicted_900 = next(
                (p["predicted_utilisation"] for p in forecast["points"] if p["horizon_sec"] == 900),
                forecast["baseline_value"],
            )
            store.forecast_snapshots[eid].append((store.cycle_number, predicted_900, forecast["baseline_value"]))

        if sources:
            store.active_forecast_source = max(sources, key=lambda s: sources[s])
        store.pressure_timeline = self._build_pressure_timeline()
        for series in store.forecast_errors.values():
            del series[:-400]

    def _build_pressure_timeline(self) -> list[dict]:
        """01 §3.4 — only entities crossing critical within 3600s, most urgent first."""
        store = self.store
        items = []
        for eid, forecast in store.forecasts.items():
            ttc = forecast.get("time_to_critical_sec")
            if ttc is None:
                continue
            state = store.entity_states.get(eid)
            if not state:
                continue
            items.append({
                "entity_id": eid,
                "display_name": store.nodes[eid]["display_name"],
                "current_utilisation": state["utilisation"],
                "current_band": state["risk_band"],
                "time_to_critical_sec": int(ttc),
                "trajectory": self._trajectory(state["utilisation"], forecast),
            })
        items.sort(key=lambda i: (i["time_to_critical_sec"], -i["current_utilisation"], i["entity_id"]))
        return items

    def _trajectory(self, current: float, forecast: dict) -> list[dict]:
        anchors = [(0, current)] + [(p["horizon_sec"], p["predicted_utilisation"]) for p in forecast["points"]]
        out = []
        for offset in TRAJECTORY_OFFSETS:
            value = anchors[-1][1]
            for (h0, v0), (h1, v1) in zip(anchors, anchors[1:]):
                if h0 <= offset <= h1:
                    frac = 0.0 if h1 == h0 else (offset - h0) / (h1 - h0)
                    value = v0 + frac * (v1 - v0)
                    break
            out.append({"horizon_sec": offset, "utilisation": round(value, 4)})
        return out

    # --- step 5 ----------------------------------------------------------------------------
    def _apply_risk(self, scores: dict[str, dict]) -> None:
        store = self.store
        for eid, result in scores.items():
            state = store.entity_states.get(eid)
            if not state:
                continue
            state["risk_score"] = int(result["risk_score"])
            state["risk_band"] = result["risk_band"]
            store.risk_breakdown[eid] = result.get("breakdown", [])

    def _recompute_summary(self) -> None:
        store = self.store
        states = store.entity_states
        zone_utils = [states[z]["utilisation"] for z in store.zone_ids() if z in states]
        load_variance = round(variance(zone_utils), 4)
        scores = [s["risk_score"] for s in states.values()]
        overall = int(round(sum(scores) / len(scores))) if scores else 0
        worst = max(scores) if scores else 0
        overall = int(round(0.4 * overall + 0.6 * worst))
        store.summary = {
            "overall_risk_score": overall,
            "overall_risk_band": band_from_score(overall, self.config.raw["thresholds"]["risk_bands"]),
            "critical_count": sum(1 for s in states.values() if s["risk_band"] == "critical"),
            "high_count": sum(1 for s in states.values() if s["risk_band"] == "high"),
            "load_variance": load_variance,
        }

    # --- step 7 -------------------------------------------------------------------------------
    def _critical_roots(self, node_state: dict[str, dict]) -> list[str]:
        roots = [e for e, s in node_state.items()
                 if s.get("risk_band") == "critical" and s.get("entity_type") != "hotel"]
        roots.sort(key=lambda e: -node_state[e].get("risk_score", 0))
        return roots[: int(self.config.raw.get("cascade", {}).get("max_roots", 5))]

    def _apply_cascades(self, cascades: list[dict]) -> list[dict]:
        store = self.store
        store.cascades = {}
        active_roots: set[str] = set()
        newly_active = []
        for c in cascades or []:
            store.cascades[c["root_entity_id"]] = c
            if c["total_downstream_failures"] >= 1:
                active_roots.add(c["root_entity_id"])
                if c["root_entity_id"] not in store.previously_active_cascade_roots:
                    newly_active.append(c)
                self._schedule_cascade_checks(c)
        store.previously_active_cascade_roots = active_roots
        if cascades:
            store.active_cascade_source = cascades[0]["source"]
        return newly_active

    def _schedule_cascade_checks(self, cascade: dict) -> None:
        store = self.store
        pending_entities = {eid for _, eid in store.cascade_pending_checks}
        for step in cascade["steps"][1:]:
            eid = step["entity_id"]
            # Keep the *first* prediction inside the look-back window: a cascade
            # re-predicted every cycle must not reset its own lead time (that
            # made every caught transition look unpredicted).
            first = store.cascade_predicted_at.get(eid)
            lookback = max(1, self.CASCADE_RECALL_LOOKBACK_SEC // self.sim_dt)
            if first is None or store.cycle_number - first > lookback:
                store.cascade_predicted_at[eid] = store.cycle_number
            if eid in pending_entities:
                continue
            due = store.cycle_number + max(1, round(step["eta_sec"] / self.sim_dt))
            store.cascade_pending_checks.append((due, eid))
            pending_entities.add(eid)

    def _resolve_cascade_predictions(self) -> None:
        store = self.store
        remaining: deque[tuple[int, str]] = deque()
        for due, eid in store.cascade_pending_checks:
            if due > store.cycle_number:
                remaining.append((due, eid))
                continue
            state = store.entity_states.get(eid)
            band = state["risk_band"] if state else "low"
            if band in ("high", "critical"):
                store.cascade_eval["alerts_confirmed"] += 1
            else:
                store.cascade_eval["alerts_false"] += 1
        store.cascade_pending_checks = remaining

    CASCADE_RECALL_LOOKBACK_SEC = 3600

    # What a cascade claims to predict: a downstream entity of these types
    # crossing into critical (the same definition the model is evaluated on
    # offline, ML/cascade_eval_v2.json). Venues, zones, parking and hotels fill
    # from their own demand, not from a cascade, so they are not scored here.
    CASCADE_SCORED_TYPES = ("gate", "road", "transport_node", "emergency_facility")

    def _track_cascade_recall(self, previous_states: dict[str, dict]) -> None:
        """Recall: of the non-root entities that just turned critical, how
        many had been predicted in advance by a cascade?"""
        store = self.store
        lookback_cycles = max(1, self.CASCADE_RECALL_LOOKBACK_SEC // self.sim_dt)
        roots = set(store.cascades)
        for eid, state in store.entity_states.items():
            if state["risk_band"] != "critical":
                continue
            if store.nodes[eid]["entity_type"] not in self.CASCADE_SCORED_TYPES:
                continue
            prev = previous_states.get(eid)
            if bool(prev) and prev["risk_band"] == "critical":
                continue
            predicted_cycle = store.cascade_predicted_at.get(eid)
            if predicted_cycle is not None and 1 <= store.cycle_number - predicted_cycle <= lookback_cycles:
                store.cascade_eval["events_caught"] += 1
                # Measured lead time: how long before the crossing it was predicted.
                store.cascade_lead_times.append(float((store.cycle_number - predicted_cycle) * self.sim_dt))
            elif eid not in roots:
                # An unpredicted root is where a cascade starts, not a failure
                # the cascade model was asked to foresee.
                store.cascade_eval["events_missed"] += 1

    def _cascade_exposure(self) -> dict[str, float]:
        store = self.store
        counts: dict[str, float] = {}
        for cascade in store.cascades.values():
            root = cascade["root_entity_id"]
            counts[root] = max(counts.get(root, 0.0), cascade["total_downstream_failures"])
            for step in cascade["steps"][1:]:
                counts[step["entity_id"]] = max(counts.get(step["entity_id"], 0.0), 1.0)
        if not counts:
            return {}
        peak = max(counts.values()) or 1.0
        return {e: v / peak for e, v in counts.items()}

    # --- step 8 -----------------------------------------------------------------------------------
    def current_compliance(self) -> float:
        """Share of visitors expected to follow an instruction: the configured
        prior, updated by every attendee nudge answer (Bayesian-style average)."""
        prior = float(self.icfg.get("default_compliance", 0.6))
        answers = self.store.observed_compliance
        return round((prior * PRIOR_COMPLIANCE_WEIGHT + sum(1 for a in answers if a))
                     / (PRIOR_COMPLIANCE_WEIGHT + len(answers)), 4)

    def optimiser_context(self) -> dict[str, Any]:
        from .accommodation import cluster_availability

        with self.world_lock:
            has = hasattr(self.generator, "closed_entities")
            closed = sorted(self.generator.closed_entities()) if has else []
            venue_gates = ({v: self.generator.gates_of_venue(v) for v, n in self.store.nodes.items()
                            if n["entity_type"] in ("venue", "zone")} if has else {})
            venue_gates = {v: g for v, g in venue_gates.items() if g}
            events = []
            if hasattr(self.generator, "event_states"):
                now_min = self.generator._elapsed_sec / 60.0 if hasattr(self.generator, "_elapsed_sec") else 0.0
                for ev_id, ev in self.generator.event_states().items():
                    att = float(ev.get("attendance") or 0.0)
                    if ev_id not in self.events.events:
                        continue    # pop-up scenario events have no schedule to change
                    name = self.events.events[ev_id]["name"]
                    events.append({
                        "event_id": ev_id, "name": name, "venue": ev["venue"], "cancelled": ev["cancelled"],
                        "minutes_to_start": round(ev["start_min"] - now_min, 1),
                        "arrived_share": round(ev["arrived"] / att, 3) if att > 0 else 1.0,
                    })
        return {
            "closed": closed,
            "venue_gates": venue_gates,
            "events": events,
            "hotel_availability": cluster_availability(self) if hasattr(self.generator, "properties_state") else {},
        }

    def intervention_ttl_sec(self) -> int:
        """Long enough to read and act on at the current speed, never stale in sim time."""
        ttl = float(self.icfg.get("ttl_sec", 1200))
        wall = float(self.icfg.get("min_wall_visible_sec", 120)) * max(self.speed_multiplier, 1.0)
        cap = float(self.icfg.get("max_ttl_sec", 3600))
        return int(min(max(ttl, wall), max(cap, ttl)))

    async def _maybe_generate_interventions(self, node_state: dict[str, dict], sim_time: str) -> list[dict]:
        store = self.store
        proposed = [i for i in store.interventions.values() if i["status"] == "proposed"]
        if len(proposed) >= int(self.icfg.get("max_live_proposals", 8)):
            return []
        busy = {i["triggered_by_entity_id"] for i in store.interventions.values()
                if i["status"] in ("proposed", "executing")}

        # Trigger: an entity predicted critical within the hour, most urgent first.
        triggers = [
            (eid, f) for eid, f in store.forecasts.items()
            if f.get("time_to_critical_sec") is not None and f["time_to_critical_sec"] <= 3600
            and eid not in busy
            and store.cycle_number - store.root_cooldown.get(eid, -10**6) >= ROOT_COOLDOWN_CYCLES
        ]
        if not triggers:
            return []
        triggers.sort(key=lambda t: (t[1]["time_to_critical_sec"], -node_state[t[0]]["risk_score"], t[0]))
        root = triggers[0][0]

        risk_context = {
            "root_entity_id": root,
            "cascade": store.cascades.get(root),
            "node_state": node_state,
            "edges": store.edges,
            "sim_time": sim_time,
            **self.optimiser_context(),
        }
        candidates, _, _ = await call_ml(
            "optimiser.generate",
            self.registry.optimiser.generate,
            self.registry.optimiser.fallback,
            self.config.budget_sec("optimise"),
            risk_context,
            self.config.raw["optimiser"]["max_candidates"],
        )
        candidates = candidates or []
        if not candidates:
            return []

        # Simulate each candidate on a clone of the live city.
        if hasattr(self.generator, "clone"):
            from .evaluation import evaluate_candidates

            try:
                await asyncio.wait_for(
                    asyncio.to_thread(evaluate_candidates, self, candidates, root, self.current_compliance()),
                    timeout=self.config.budget_sec("evaluate"),
                )
            except asyncio.TimeoutError:
                log.warning("candidate evaluation exceeded its budget; keeping template estimates")
            except Exception:
                log.exception("candidate evaluation failed; keeping template estimates")
            # Simulation showed these would not help: do not put them in front of
            # an operator. If nothing helps, escalate (notify_only) instead.
            min_relief = float(self.icfg.get("min_relief_pct", 1.0))
            useful = [c for c in candidates if c.get("evaluation") is None or c["estimated_relief_pct"] >= min_relief]
            if not useful:
                useful = self.registry.optimiser.fallback(risk_context, 1)
                for c in useful:
                    c["estimated_relief_pct"] = 0.0
            candidates = useful

        ttl = self.intervention_ttl_sec()
        for candidate in candidates:
            certificate, degraded, _ = await call_ml(
                "equilibrium.certify",
                self.registry.equilibrium.certify,
                self.registry.equilibrium.fallback,
                self.config.budget_sec("certify"),
                candidate,
                node_state,
                store.edges,
                store.segments,
            )
            candidate["certificate"] = certificate
            candidate["created_at"] = sim_time
            candidate.pop("_ttl_sec", None)
            candidate["expires_at"] = shift(sim_time, ttl)
            self._score_certificate_accuracy(candidate, certificate)

        ranked = self.registry.optimiser.rank(candidates)
        for i in ranked:
            store.interventions[i["intervention_id"]] = i
            cert = i.get("certificate")
            if cert:
                store.certificates[i["intervention_id"]] = cert
        self._count_unstable_caught(ranked)
        return ranked

    def _count_unstable_caught(self, ranked: list[dict]) -> None:
        """01 §3.10 — how many UNSTABLE options a relief-only ranking would have picked."""
        if not ranked:
            return
        by_relief = max(ranked, key=lambda i: float(i.get("estimated_relief_pct", 0.0)))
        verdict = (by_relief.get("certificate") or {}).get("verdict")
        if verdict == "UNSTABLE" and ranked[0]["intervention_id"] != by_relief["intervention_id"]:
            self.store.unstable_caught += 1

    def _score_certificate_accuracy(self, candidate: dict, certificate: dict | None) -> None:
        """03 §5.6 — does the certificate agree with an independent simulation?

        The certificate says an action holds (STABLE/CONDITIONAL) or not
        (UNSTABLE); the simulated evaluation says whether it relieved its root
        without creating a new critical entity. Agreement is recorded; this is
        the only writer of `certificates_scored`."""
        ev = candidate.get("evaluation")
        if not ev or not certificate or candidate["intervention_type"] == "notify_only":
            return
        sim_ok = candidate["estimated_relief_pct"] > 0 and not ev.get("new_critical_entities")
        cert_ok = certificate.get("verdict") != "UNSTABLE"
        self.store.certificates_scored.append(sim_ok == cert_ok)

    # --- approval (01 §3.6) -----------------------------------------------------------------------
    def approve_intervention(self, item: dict) -> dict:
        """Apply an approved intervention to the city for real.

        The live world is forked first (the do-nothing counterfactual); then the
        action is applied to the live world and to the twin's nominal model
        (an approved action is part of the announced plan)."""
        store = self.store
        compliance = self.current_compliance()
        duration = float(self.icfg.get("effect_duration_sec", 2700))
        root = item.get("triggered_by_entity_id") or (item["target_entity_ids"] or [None])[0]
        with self.world_lock:
            if hasattr(self.generator, "clone"):
                self.counterfactuals[item["intervention_id"]] = self.generator.clone()
            if item["intervention_type"] == "event_delay":
                # A delay is an announced schedule change: it goes on the
                # schedule itself (so /events, attendee plans and every world
                # see it), except the do-nothing counterfactual just forked.
                action = item.get("_action") or {}
                self.events.update(action["event_id"], store.sim_time,
                                   delay_sec=int(float(action.get("delay_min", 20)) * 60))
                schedule = self.events.to_generator()
                cf = self.counterfactuals.get(item["intervention_id"])
                for w in self._all_worlds():
                    if w is not cf and hasattr(w, "set_events"):
                        w.set_events(schedule)
                mod_id = None
                item["_event_id"] = action["event_id"]
            elif hasattr(self.generator, "apply_intervention"):
                mod_id = self.generator.apply_intervention(item, compliance, duration)
                if self.nominal is not None:
                    self.nominal.apply_intervention(item, compliance, duration)
            else:
                mod_id = None
                self.generator.apply_relief(item["target_entity_ids"], float(item.get("estimated_relief_pct", 0.0)) / 100.0)
        watch = [e for e in dict.fromkeys([root] + list(item["target_entity_ids"])) if e in store.entity_states]
        item["status"] = "executing"
        item["_applied_at"] = store.sim_time
        item["_compliance"] = compliance
        item["_modifier_id"] = mod_id
        item["_root"] = root
        item["_util_at_approval"] = {e: store.entity_states[e]["utilisation"] for e in watch}
        self.world_changed()
        return {"branch_id": f"cf_{item['intervention_id']}", "compliance": compliance}

    def record_compliance(self, accepted: bool) -> float:
        self.store.observed_compliance.append(bool(accepted))
        c = self.current_compliance()
        with self.world_lock:
            for w in self._all_worlds():
                if hasattr(w, "set_compliance"):
                    w.set_compliance(c)
        self.world_changed()
        return c

    # --- settlement --------------------------------------------------------------------------
    async def _settle_executing_interventions(self, sim_time: str) -> list[dict]:
        """An executing intervention becomes a regret-ledger entry once its
        effect has had `settle_delay_sec` to act. Realised relief compares the
        live city with its do-nothing counterfactual at the same moment."""
        store = self.store
        settle_delay = float(self.icfg.get("settle_delay_sec", 900))
        settled = []
        for i in list(store.interventions.values()):
            if i["status"] != "executing":
                continue
            applied = i.get("_applied_at")
            if not applied or (parse(sim_time) - parse(applied)).total_seconds() < settle_delay:
                continue
            iid = i["intervention_id"]
            root = i.get("_root")
            with self.world_lock:
                cf = self.counterfactuals.pop(iid, None)
                live_util = self.generator.utilisation() if hasattr(self.generator, "utilisation") else {}
                cf_util = cf.utilisation() if cf is not None else {}
            at_approval = i.get("_util_at_approval", {})
            baseline = float(at_approval.get(root, 0.0))
            actual = float(live_util.get(root, store.entity_states.get(root, {}).get("utilisation", baseline)))
            do_nothing = float(cf_util.get(root, actual))

            realised = (do_nothing - actual) / do_nothing * 100.0 if do_nothing > 0.05 else 0.0
            natural = (baseline - do_nothing) / baseline * 100.0 if baseline > 0.05 else 0.0
            realised = round(clamp(realised, -100.0, 100.0), 1)
            natural = round(clamp(natural, -100.0, 100.0), 1)

            zones = [e for e, n in store.nodes.items() if n["entity_type"] == "zone"]
            non_hotel = [e for e, n in store.nodes.items() if n["entity_type"] != "hotel"]
            store.settlements.append({
                "intervention_id": iid,
                "root": root,
                "root_actual": round(actual, 4),
                "root_counterfactual": round(do_nothing, 4),
                "peak_actual": round(max((live_util.get(e, 0.0) for e in non_hotel), default=0.0), 4),
                "peak_counterfactual": round(max((cf_util.get(e, 0.0) for e in non_hotel), default=0.0), 4),
                "zone_variance_actual": round(variance([live_util.get(z, 0.0) for z in zones]), 5),
                "zone_variance_counterfactual": round(variance([cf_util.get(z, 0.0) for z in zones]), 5),
            })
            for t in i["target_entity_ids"]:
                if t in cf_util:
                    store.counterfactual_utilisation[t] = float(cf_util[t])

            i["status"] = "completed"
            entry = {
                "regret_id": f"reg_{len(store.regret_entries) + 1:04d}",
                "intervention_id": iid,
                "intervention_type": i["intervention_type"],
                "predicted_relief_pct": float(i["estimated_relief_pct"]),
                "realised_relief_pct": realised,
                "counterfactual_relief_pct": natural,
                "regret": round(float(i["estimated_relief_pct"]) - realised, 2),
                "sim_time": sim_time,
            }
            store.regret_entries.append(entry)
            settled.append(entry)
        return settled

    # --- step 2 -------------------------------------------------------------------------------------
    def _persist(self, sim_time: str) -> None:
        from ..db import models
        from ..db.base import SessionLocal

        ts = parse(sim_time)
        store = self.store
        try:
            with SessionLocal() as session:
                session.bulk_save_objects([
                    models.EntityState(
                        entity_id=s["entity_id"], sim_time=ts,
                        current_count=s["current_count"], utilisation=s["utilisation"],
                        flow_rate_per_min=s["flow_rate_per_min"], risk_score=s["risk_score"],
                        risk_band=s["risk_band"], is_observed=s["is_observed"],
                    )
                    for s in store.entity_states.values()
                ])
                # Forecasts are persisted every 10th cycle: the full set every
                # 30s grew the database by hundreds of MB per day of sim time.
                if store.cycle_number % 10 == 0:
                    session.bulk_save_objects([
                        models.Forecast(
                            entity_id=eid, generated_at=ts, source=f["source"],
                            horizon_sec=p["horizon_sec"], predicted_utilisation=p["predicted_utilisation"],
                            lower_90=p.get("lower_90"), upper_90=p.get("upper_90"),
                            time_to_critical_sec=f.get("time_to_critical_sec"),
                        )
                        for eid, f in store.forecasts.items() for p in f["points"]
                    ])
                for i in store.interventions.values():
                    if i.get("_persisted_status") == i["status"]:
                        continue
                    session.merge(models.Intervention(
                        intervention_id=i["intervention_id"], intervention_type=i["intervention_type"],
                        status=i["status"], target_entity_ids=i["target_entity_ids"],
                        triggered_by_entity_id=i.get("triggered_by_entity_id"),
                        title=i["title"], description=i["description"],
                        estimated_relief_pct=i["estimated_relief_pct"],
                        estimated_cost_paise=i["estimated_cost_paise"],
                        estimated_delay_sec=i["estimated_delay_sec"], feasibility=i["feasibility"],
                        rank_score=i["rank_score"], created_at=parse(i["created_at"]),
                        expires_at=parse(i["expires_at"]),
                    ))
                    cert = store.certificates.get(i["intervention_id"])
                    if cert:
                        session.merge(models.Certificate(
                            certificate_id=cert["certificate_id"], intervention_id=i["intervention_id"],
                            verdict=cert["verdict"], converged=cert["converged"],
                            iterations=cert["iterations"], post_nudge_variance=cert["post_nudge_variance"],
                            baseline_variance=cert["baseline_variance"],
                            max_zone_utilisation=cert["max_zone_utilisation"],
                            max_zone_entity_id=cert.get("max_zone_entity_id"),
                            oscillation_risk=cert["oscillation_risk"],
                            compliance_sensitivity=cert["compliance_sensitivity"],
                            compliance_sweep=cert["compliance_sweep"], reason=cert["reason"],
                        ))
                    i["_persisted_status"] = i["status"]
                for entry in store.regret_entries:
                    session.merge(models.RegretEntry(
                        regret_id=entry["regret_id"], intervention_id=entry["intervention_id"],
                        intervention_type=entry["intervention_type"],
                        predicted_relief_pct=entry["predicted_relief_pct"],
                        realised_relief_pct=entry["realised_relief_pct"],
                        counterfactual_relief_pct=entry["counterfactual_relief_pct"],
                        regret=entry["regret"], sim_time=parse(entry["sim_time"]),
                    ))
                for n in store.nudges.values():
                    session.merge(models.Nudge(
                        nudge_id=n["nudge_id"], attendee_id=n["attendee_id"],
                        intervention_id=n.get("intervention_id"), segment_id=n.get("segment_id"),
                        headline=n["headline"], body=n["body"], tradeoff=n["tradeoff"],
                        target_entity_id=n.get("target_entity_id"), status=n["status"],
                        issued_at=parse(n["issued_at"]), expires_at=parse(n["expires_at"]),
                    ))
                session.commit()
        except Exception:
            log.exception("persistence failed for cycle %s", store.cycle_number)

    def _write_cache(self) -> None:
        store = self.store
        CACHE.set("state:current", self.state_payload(), TTL["state:current"])
        CACHE.set("forecast:latest", self.forecast_payload(), TTL["forecast:latest"])
        CACHE.set("cascade:active", self.cascade_payload(), TTL["cascade:active"])
        fidelity = store.twin_fidelity_payload()
        if fidelity:
            CACHE.set("twin:fidelity", fidelity, TTL["twin:fidelity"])

    # --- step 9 -----------------------------------------------------------------------------------------
    def operations_summary(self) -> dict[str, Any]:
        ops = self.store.operations or {}
        return {k: ops.get(k) for k in (
            "rooms_available", "rooms_unmet", "saturated_properties", "queued_people", "late_entries",
            "diverted_people", "current_travel_time_sec", "arrivals_per_min", "egress_per_min",
        )} | {"compliance": self.current_compliance(), "disruptions": len(self.store.disruptions)}

    async def _broadcast(
        self,
        previous_states: dict[str, dict],
        fidelity: dict | None,
        anomalies: list[dict],
        new_cascades: list[dict],
        new_interventions: list[dict],
        expired: list[dict],
        settled: list[dict],
        sim_time: str,
    ) -> None:
        store = self.store
        await MANAGER.broadcast(
            "tick",
            {"cycle_number": store.cycle_number, "sim_time": sim_time, "summary": store.summary,
             "operations": self.operations_summary()},
            sim_time,
        )
        changed = store.changed_entities(previous_states)
        if changed:
            await MANAGER.broadcast("state_update", {"entities": changed}, sim_time)
        await MANAGER.broadcast(
            "forecast_update",
            {"active_source": store.active_forecast_source, "pressure_timeline": store.pressure_timeline},
            sim_time,
        )
        for cascade in new_cascades:
            await MANAGER.broadcast("cascade_alert", {"cascade": cascade}, sim_time)
        for i in new_interventions:
            await MANAGER.broadcast("intervention_queued", {"intervention": _public(i)}, sim_time)
        for i in expired:
            await MANAGER.broadcast("intervention_resolved", {"intervention_id": i["intervention_id"], "status": "expired"}, sim_time)
        if settled:
            from .metrics import build_regret

            summary = build_regret(self)["summary"]
            for entry in settled:
                await MANAGER.broadcast("intervention_resolved",
                                        {"intervention_id": entry["intervention_id"], "status": "completed"}, sim_time)
                await MANAGER.broadcast("regret_update", {"entry": entry, "summary": summary}, sim_time)
        if fidelity:
            fidelity["sim_time"] = sim_time
            fidelity["drift_mode_enabled"] = store.drift_mode_enabled
            store.twin_fidelity = fidelity
            store.append_twin_history(fidelity)
            await MANAGER.broadcast("twin_fidelity", store.twin_fidelity_payload() or {}, sim_time)
        for a in anomalies[:3]:
            store.anomalies.append({**a, "sim_time": sim_time})
            await MANAGER.broadcast("anomaly", a, sim_time)

    # --- backpressure ------------------------------------------------------------------------------------
    def _maybe_shed_load(self, total_ms: float) -> None:
        cap = self.config.budget_ms("total_cap")
        if total_ms > cap:
            self._shed_forecast_until = self.store.cycle_number + 2
            log.warning("cycle took %.0fms (cap %dms); shedding forecast for 2 cycles", total_ms, cap)

    # --- payload builders shared with the REST layer ---------------------------------------------------------
    def state_payload(self) -> dict[str, Any]:
        store = self.store
        return {
            "sim_time": store.sim_time,
            "cycle_number": store.cycle_number,
            "entities": store.entity_states_list(),
            "summary": store.summary,
        }

    def forecast_payload(self, entity_ids: list[str] | None = None) -> dict[str, Any]:
        store = self.store
        return {
            "generated_at": store.sim_time,
            "active_source": store.active_forecast_source,
            "forecasts": [f for eid, f in store.forecasts.items() if entity_ids is None or eid in entity_ids],
        }

    def cascade_payload(self) -> dict[str, Any]:
        store = self.store
        return {"sim_time": store.sim_time, "source": store.active_cascade_source,
                "cascades": list(store.cascades.values())}

    def last_cycle_ms(self) -> float:
        return self._last_cycle_ms

    # --- events (schedule changes) ---------------------------------------------------------------------------
    def event_views(self) -> list[dict[str, Any]]:
        with self.world_lock:
            live = self.generator.event_states() if hasattr(self.generator, "event_states") else {}
        return [
            self.events.view(ev, self.store.sim_time, live.get(ev["event_id"]),
                             self.store.nodes.get(ev["venue_entity_id"], {}).get("display_name"))
            for ev in sorted(self.events.events.values(), key=lambda e: e["start_time"])
        ]

    def event_view(self, event_id: str) -> dict[str, Any]:
        ev = self.events.get(event_id)
        return next(v for v in self.event_views() if v["event_id"] == ev["event_id"])

    def twin_view(self, entity_id: str) -> dict[str, Any]:
        """Every layer the twin holds for one entity (see `schemas.TwinView`)."""
        store = self.store
        layers = dict(store.twin_layers.get(entity_id) or {})
        util = float(store.entity_states.get(entity_id, {}).get("utilisation", 0.0))
        fc = store.forecasts.get(entity_id) or {}
        f1800 = next((p["predicted_utilisation"] for p in fc.get("points", []) if p["horizon_sec"] == 1800), None)
        cfs = []
        with self.world_lock:
            plan = self.nominal.utilisation().get(entity_id) if self.nominal is not None else None
            for iid, world in self.counterfactuals.items():
                if store.interventions.get(iid, {}).get("status") == "executing":
                    cfs.append({"intervention_id": iid,
                                "utilisation": round(float(world.utilisation().get(entity_id, 0.0)), 4)})
        layers.update({
            "plan_utilisation": None if plan is None else round(float(plan), 4),
            "forecast_1800": f1800,
            "over_capacity": util > 1.0,
            "over_capacity_pct": round(max(0.0, util - 1.0) * 100.0, 1),
            "counterfactuals": cfs,
        })
        return layers

    def update_event(self, event_id: str, **changes: Any) -> dict[str, Any]:
        """Apply a schedule change to every world (it is announced, so the twin's
        nominal model and the counterfactuals all see it)."""
        self.events.update(event_id, self.store.sim_time, **changes)
        schedule = self.events.to_generator()
        with self.world_lock:
            for w in self._all_worlds():
                if hasattr(w, "set_events"):
                    w.set_events(schedule)
        self.world_changed()
        return self.event_view(event_id)

    # --- live disruptions ---------------------------------------------------------------------------------------
    def inject_disruption(self, scenario_type: str, params: dict, label: str | None = None) -> dict[str, Any]:
        """A real-world incident. Unannounced: the twin's nominal model does not
        know about it — assimilation has to find it from the sensors."""
        store = self.store
        did = f"dis_{len(store.disruptions) + 1:04d}"
        while did in store.disruptions:
            did = did + "x"
        with self.world_lock:
            for w in [self.generator] + list(self.counterfactuals.values()):
                w.inject(scenario_type, params, source="disruption", modifier_id=did)
        record = {
            "disruption_id": did, "scenario_type": scenario_type, "params": dict(params or {}),
            "label": label, "started_at": store.sim_time,
        }
        store.disruptions[did] = record
        self.world_changed()
        return record

    def clear_disruption(self, disruption_id: str) -> dict[str, Any]:
        from ..errors import ApiError

        record = self.store.disruptions.get(disruption_id)
        if not record:
            raise ApiError("DISRUPTION_NOT_FOUND", f"No disruption with id '{disruption_id}'.",
                           {"disruption_id": disruption_id})
        with self.world_lock:
            for w in [self.generator] + list(self.counterfactuals.values()):
                w.remove_modifier(disruption_id)
        self.store.disruptions.pop(disruption_id)
        self.world_changed()
        return record

    # --- demo control (01 §3.13) --------------------------------------------------------------------------------
    async def demo_control(
        self,
        action: str | None,
        seed: int | None,
        speed_multiplier: float | None,
        seek_to_sim_time: str | None,
        inject: dict | None,
    ) -> dict[str, Any]:
        if seed is not None:
            self.seed = seed
        if speed_multiplier is not None:
            self.speed_multiplier = float(clamp(float(speed_multiplier), 0.5, 600.0))

        if action == "pause":
            self.paused = True
        elif action == "play":
            self.paused = False
        elif action == "reset":
            await self._reset()
        elif action == "seek" and seek_to_sim_time:
            elapsed = (parse(seek_to_sim_time) - parse(self.store.sim_start)).total_seconds()
            with self.world_lock:
                self.generator.seek(max(0.0, elapsed))
                if self.nominal is not None:
                    self.nominal.seek(max(0.0, elapsed))
                self.counterfactuals.clear()
            self.store.reset_clock(seek_to_sim_time)
            self.store.cycle_number = int(max(0.0, elapsed) // self.sim_dt)
            self.world_changed()

        if inject:
            self.inject_disruption(inject["scenario_type"], inject.get("params", {}))

        return {
            "status": "paused" if self.paused else "playing",
            "sim_time": self.store.sim_time,
            "seed": self.seed,
            "speed_multiplier": self.speed_multiplier,
        }

    def prime_state(self) -> None:
        """Populate entity states and the summary from the city model's current
        instant without advancing the clock, so a freshly started or reset run
        (possibly paused) shows the real initial city, not an empty map or the
        previous run's risk summary."""
        store = self.store
        with self.world_lock:
            truth = self.generator.ground_truth()
        observations = {e: v["current_count"] for e, v in truth.items() if v.get("is_observed")}
        estimates = {e: {"current_count": v["current_count"]} for e, v in truth.items()}
        self._merge_states(observations, estimates, truth, store.sim_time)
        scores = self.registry.risk.score(store.node_state_for_ml(), store.forecasts, {})
        self._apply_risk(scores or {})
        self._recompute_summary()

    async def _reset(self) -> None:
        """Full reproducible reset — the rehearsal depends on this being exact."""
        self.store.clear_live()
        self.store.reset_clock(self.store.sim_start)
        self.events.reset()
        with self.world_lock:
            self._build_worlds()
        self.prime_state()
        for h in self.store.history.values():
            h.clear()  # the primed t=0 reading is not a forecasting observation
        self.store.drift_mode_enabled = False
        CACHE.clear()
        self.world_changed()
        if self.commander is not None and hasattr(self.commander, "_cache"):
            self.commander._cache.clear()

        from ..db.seed import clear_run_tables, persist_events

        await asyncio.to_thread(clear_run_tables)
        await asyncio.to_thread(persist_events, self.events.to_generator())
        log.info("demo reset to seed %s", self.seed)

    def set_drift_mode(self, enabled: bool) -> dict[str, Any]:
        self.store.drift_mode_enabled = enabled
        started = self.store.sim_time if enabled else None
        if hasattr(self.registry.twin, "set_drift_mode"):
            self.registry.twin.set_drift_mode(enabled, started)
        if enabled:
            self.store.twin_history.clear()
        return {"drift_mode_enabled": enabled, "uncorrected_ensemble_started_at": started}


def _public(intervention: dict) -> dict:
    return {k: v for k, v in intervention.items() if not k.startswith("_")}


ENGINE: Engine | None = None


def get_engine() -> Engine:
    if ENGINE is None:  # pragma: no cover - guarded by app lifespan
        raise RuntimeError("engine not started")
    return ENGINE


def set_engine(engine: Engine | None) -> None:
    global ENGINE
    ENGINE = engine
