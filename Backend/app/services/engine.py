"""The orchestration cycle (01_BACKEND_CONTRACT.md §2).

    1. generator.tick()                     -> observations
    2. persist observations                 -> Postgres + cache
    3. twin.assimilate(observations)        -> corrected ensemble
    4. forecaster.predict(entities)         -> Forecast[]
    5. risk_scorer.score(state, forecast)   -> risk_score per entity
    6. anomaly.detect(residuals)            -> anomaly flags
    7. cascade.predict(graph_state)         -> CascadeResult[]
    8. IF any predicted band critical within 3600s: optimise + certify + queue
    9. broadcast WS events
   10. write cycle metrics

Backpressure rule (§2): skip the forecast refresh before you skip assimilation.
Drift compounds; a stale forecast does not. That ordering is enforced in
`_maybe_shed_load`, not left to whoever is on call.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import time
from collections import deque
from typing import Any

from ..cache import CACHE, TTL
from ..config import get_config
from ..ml_reference.common import band_from_score, clamp, variance
from ..ml_registry import MLRegistry, call_ml
from ..simtime import iso, parse, shift
from ..ws.manager import MANAGER
from .state_store import StateStore

log = logging.getLogger("eventflow.cycle")

# 01 §3.4 — the pressure timeline is always these six offsets.
TRAJECTORY_OFFSETS = [0, 300, 600, 900, 1200, 1800]
MIN_WALL_SLEEP = 0.2
# The inter-cycle wait is re-evaluated in slices this long, so a speed change or
# a pause takes effect within ~0.1 s instead of after the previous sleep ends
# (a 0.01x speed would otherwise commit the loop to a ~50-minute sleep).
WAKE_CHECK_SEC = 0.1
# Operator queue ceiling — the frontend shows 10; beyond that it is noise.
MAX_LIVE_PROPOSALS = 8


class Engine:
    """Owns the clock, the ML registry, and the only writer to StateStore."""

    def __init__(self) -> None:
        self.config = get_config()
        event_cfg = self.config.raw["event"]

        self.seed = self.config.demo_seed
        self.speed_multiplier = validated_speed(self.config.raw.get("speed_multiplier", 60))
        self.sim_dt = self.config.cycle_sec
        self.paused = False
        # Timeline epoch: bumped on every reset. Anything cached against the
        # previous timeline (Commander answers, client-side UI state) is keyed
        # on it so it can never be mistaken for the current run.
        self.run_id = 1
        # Observer / judge mode (Phase 0). Off by default so the unattended
        # cycle behaves exactly as before; the Command Centre toggles it.
        self.auto_pause_on_intervention = False
        self._pause_on_next_decision = False
        self.pause_reason: dict[str, Any] | None = None

        self.store = StateStore(sim_start=event_cfg["sim_start_time"])
        self.registry = MLRegistry()
        self.generator = self.registry.build_generator(
            {"nodes": list(self.store.nodes.values()), "edges": self.store.edges}, self.seed
        )
        self._init_twin()

        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self._last_cycle_ms = 0.0
        self._shed_forecast_until = 0
        # Serialises whole cycles against control actions that must not
        # interleave with one (reset, step), since run_cycle awaits mid-way.
        self._cycle_lock = asyncio.Lock()

    # --- lifecycle ------------------------------------------------------------
    def _init_twin(self) -> None:
        caps = self.store.capacities()
        truth = self.generator.ground_truth()
        counts = {e: v["current_count"] for e, v in truth.items()}
        if hasattr(self.registry.twin, "initialise"):
            self.registry.twin.initialise(
                list(self.store.nodes.keys()), caps, counts,
                entity_types={e: n["entity_type"] for e, n in self.store.nodes.items()},
            )

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

    def wall_seconds_per_cycle(self) -> float:
        return max(MIN_WALL_SLEEP, self.sim_dt / self.speed_multiplier)

    async def _loop(self) -> None:
        while not self._stopping.is_set():
            if self.paused:
                await asyncio.sleep(WAKE_CHECK_SEC)
                continue
            started = time.perf_counter()
            try:
                async with self._cycle_lock:
                    if not self.paused:  # a pause may have landed while waiting
                        await self.run_cycle()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("cycle failed; continuing to the next one")
            # Wait out the rest of this cycle's wall interval, re-reading the
            # speed each slice so a change applies now, not after the old sleep.
            while not self._stopping.is_set() and not self.paused:
                remaining = started + self.wall_seconds_per_cycle() - time.perf_counter()
                if remaining <= 0:
                    break
                await asyncio.sleep(min(remaining, WAKE_CHECK_SEC))

    # --- the cycle -------------------------------------------------------------
    async def run_cycle(self) -> None:
        cycle_started = time.perf_counter()
        store = self.store
        previous_states = store.snapshot_states()
        store.advance(self.sim_dt)
        sim_time = store.sim_time

        # 1. observations ------------------------------------------------------
        observations = await asyncio.to_thread(self.generator.tick, self.sim_dt)
        truth = self.generator.ground_truth()

        # 3. assimilate — never skipped (§2 latency table) ----------------------
        # The forecast half of the EnKF cycle (03 §3.1/§3.2): every member is
        # advanced with process noise and mandatory covariance inflation BEFORE
        # the analysis step below corrects it. Skipping this — as this line's
        # absence used to do — means `assimilate()` only ever runs the Kalman
        # update against a never-advanced ensemble: the gain shrinks spread every
        # cycle with nothing re-injecting it, so the filter collapses (measured
        # ensemble_spread -> ~0.0001 within ~15 cycles) and quietly stops
        # correcting. `step()` has no `.fallback()` in the 03 §3.1 interface
        # because it mutates internal state only — a failure here can't corrupt
        # the wire, so it is caught and logged rather than routed through
        # call_ml's timeout/fallback machinery.
        if hasattr(self.registry.twin, "set_known_transfers"):
            self.registry.twin.set_known_transfers(self.generator.active_transfers())
        if hasattr(self.registry.twin, "step"):
            try:
                await asyncio.to_thread(self.registry.twin.step, self.sim_dt)
            except Exception:
                log.exception("twin.step failed; assimilation will run against a stale ensemble")

        fidelity, twin_degraded, assimilate_ms = await call_ml(
            "twin.assimilate",
            self.registry.twin.assimilate,
            self.registry.twin.fallback,
            # The budget is a warning line for assimilation, not a kill switch:
            # the contract says extend and log rather than drop the correction.
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
            forecasts, forecast_degraded, forecast_ms = await call_ml(
                "forecaster.predict",
                self.registry.forecaster.predict,
                self.registry.forecaster.fallback,
                self.config.budget_sec("forecast"),
                store.series(),
                store.capacities(),
                self.config.horizons_sec,
                sim_time,
            )
            self._apply_forecasts(forecasts or {}, sim_time)
        else:
            forecast_ms = 0.0
            log.info("shedding forecast refresh this cycle (backpressure)")

        # 5. risk ------------------------------------------------------------------
        node_state = store.node_state_for_ml()
        exposure = self._cascade_exposure()
        scores, _, _ = await call_ml(
            "risk.score",
            self.registry.risk.score,
            self.registry.risk.fallback,
            0.2,
            node_state,
            store.forecasts,
            exposure,
        )
        self._apply_risk(scores or {})

        # 6. anomalies --------------------------------------------------------------
        anomalies, _, _ = await call_ml(
            "anomaly.detect",
            self.registry.anomaly.detect,
            self.registry.anomaly.fallback,
            0.15,
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
        new_interventions = await self._maybe_generate_interventions(node_state, sim_time)
        expired = store.expire_interventions()
        settled = await self._settle_executing_interventions(sim_time)

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

        # 11. observer mode: stop on a REAL proposal so a human can read it -----------------
        if new_interventions and (self.auto_pause_on_intervention or self._pause_on_next_decision):
            await self._observer_pause(new_interventions[0], sim_time)

    async def _observer_pause(self, top: dict, sim_time: str) -> None:
        """Pause because the optimiser just queued a real proposal batch.

        Pausing freezes the sim clock, so the proposal's sim-time TTL cannot run
        out while the operator reads it — the intervention's own semantics
        (created_at / expires_at / approve / reject / expire) are untouched.
        """
        self.paused = True
        self._pause_on_next_decision = False
        self.pause_reason = {
            "kind": "intervention_proposed",
            "intervention_id": top["intervention_id"],
            "entity_id": top.get("triggered_by_entity_id"),
            "cycle_number": self.store.cycle_number,
            "sim_time": sim_time,
        }
        log.info("observer pause at cycle %s: %s proposed for %s",
                 self.store.cycle_number, top["intervention_id"], top.get("triggered_by_entity_id"))
        await self.broadcast_demo_status()

    # --- step 3/1: merge observations and twin estimates -------------------------------
    def _merge_states(
        self,
        observations: dict[str, float],
        twin_state: dict[str, dict],
        truth: dict[str, dict],
        sim_time: str,
    ) -> None:
        store = self.store
        for eid, node in store.nodes.items():
            # Utilisation is people / EFFECTIVE capacity (a capacity cut or a
            # closure raises it); the generator owns the effective figure.
            cap = float(self.generator.capacity(eid)) or 1.0
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

            store.entity_states[eid] = {
                "entity_id": eid,
                "sim_time": sim_time,
                "current_count": round(count, 1),
                "utilisation": utilisation,
                # 30 sim-seconds per cycle -> per-minute rate is twice the delta.
                "flow_rate_per_min": round((count - prev_count) * (60.0 / self.sim_dt), 1),
                "risk_score": int(previous["risk_score"]) if previous else 0,
                "risk_band": previous["risk_band"] if previous else "low",
                "is_observed": observed,
            }
            store.history[eid].append(utilisation)

    # --- step 4 -------------------------------------------------------------------------
    # Validate at the shortest contracted horizon (900s = §2.1). A lag this
    # short still leaves enough history within a demo run to populate the MAE,
    # and it is a real forecast lead time rather than a mismatched one.
    VALIDATION_HORIZON_SEC = 900

    def _apply_forecasts(self, forecasts: dict[str, dict], sim_time: str) -> None:
        store = self.store
        sources: dict[str, int] = {}
        lag_cycles = max(1, self.VALIDATION_HORIZON_SEC // self.sim_dt)

        for eid, raw in forecasts.items():
            if eid not in store.nodes:
                continue

            actual = store.entity_states[eid]["utilisation"]

            # Validate whichever past snapshot's horizon lands on *now* — never
            # the previous cycle's forecast, which predicted 900s ahead of a
            # point only 30s ago and is not yet due to be checked.
            snapshots = store.forecast_snapshots[eid]
            due_cycle = store.cycle_number - lag_cycles
            while snapshots and snapshots[0][0] < due_cycle:
                snapshots.popleft()  # too old to ever match again; drop it
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
            store.forecast_snapshots[eid].append(
                (store.cycle_number, predicted_900, forecast["baseline_value"])
            )

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
            items.append(
                {
                    "entity_id": eid,
                    "display_name": store.nodes[eid]["display_name"],
                    "current_utilisation": state["utilisation"],
                    "current_band": state["risk_band"],
                    "time_to_critical_sec": int(ttc),
                    "trajectory": self._trajectory(state["utilisation"], forecast),
                }
            )
        items.sort(key=lambda i: (i["time_to_critical_sec"], i["entity_id"]))
        return items

    def _trajectory(self, current: float, forecast: dict) -> list[dict]:
        """Six fixed offsets, linearly interpolated between the forecast horizons."""
        anchors = [(0, current)] + [
            (p["horizon_sec"], p["predicted_utilisation"]) for p in forecast["points"]
        ]
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
        # The headline score leans on the worst entity, not the average — an average
        # over 66 entities hides exactly the one the operator needs to see.
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
        return [e for e, s in node_state.items() if s.get("risk_band") in ("high", "critical")]

    def _apply_cascades(self, cascades: list[dict]) -> list[dict]:
        store = self.store
        store.cascades = {}
        active_roots: set[str] = set()
        newly_active = []
        for c in cascades or []:
            store.cascades[c["root_entity_id"]] = c
            if c["total_downstream_failures"] >= 1:
                active_roots.add(c["root_entity_id"])
                # H4: cascade_alert is "when a new cascade appears" (01 §4.1), not
                # every cycle a still-active one refreshes its prediction — a live
                # cascade recomputes ~every cycle and would otherwise emit and
                # restart the frontend's arc-reveal animation twice a second.
                if c["root_entity_id"] not in store.previously_active_cascade_roots:
                    newly_active.append(c)
                # Lead time: how early we saw it, relative to the root's own ETA.
                etas = [s["eta_sec"] for s in c["steps"] if s["depth"] > 0]
                if etas:
                    store.cascade_lead_times.append(float(max(etas)))

                self._schedule_cascade_checks(c)
        store.previously_active_cascade_roots = active_roots
        if cascades:
            store.active_cascade_source = cascades[0]["source"]
        return newly_active

    # --- online cascade precision/recall (03 §4.4) ------------------------------------------
    def _schedule_cascade_checks(self, cascade: dict) -> None:
        """Record every downstream (non-root) prediction so its outcome can be
        checked once its own predicted eta arrives — this is what makes
        `cascade_precision` a measurement instead of a formula."""
        store = self.store
        pending_entities = {eid for _, eid in store.cascade_pending_checks}
        for step in cascade["steps"][1:]:
            eid = step["entity_id"]
            store.cascade_predicted_at[eid] = store.cycle_number
            if eid in pending_entities:
                continue  # already have an earlier check pending for this entity
            due = store.cycle_number + max(1, round(step["eta_sec"] / self.sim_dt))
            store.cascade_pending_checks.append((due, eid))
            pending_entities.add(eid)

    def _resolve_cascade_predictions(self) -> None:
        """Precision half: of the predictions whose eta has now arrived, how many
        actually landed in high/critical band?"""
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

    # 03 §4.4's lead-time target is >=900s; a prediction older than the deepest
    # forecast horizon (3600s) is no longer a meaningful "advance warning" for
    # whatever just happened, so it does not count as a caught event.
    CASCADE_RECALL_LOOKBACK_SEC = 3600

    def _track_cascade_recall(self, previous_states: dict[str, dict]) -> None:
        """Recall half: of the entities that just transitioned into high/critical,
        how many had been predicted in advance by any cascade?"""
        store = self.store
        lookback_cycles = max(1, self.CASCADE_RECALL_LOOKBACK_SEC // self.sim_dt)
        for eid, state in store.entity_states.items():
            if state["risk_band"] not in ("high", "critical"):
                continue
            prev = previous_states.get(eid)
            was_high = bool(prev) and prev["risk_band"] in ("high", "critical")
            if was_high:
                continue  # not a fresh transition — already counted when it first happened
            predicted_cycle = store.cascade_predicted_at.get(eid)
            if predicted_cycle is not None and 1 <= store.cycle_number - predicted_cycle <= lookback_cycles:
                store.cascade_eval["events_caught"] += 1
            else:
                store.cascade_eval["events_missed"] += 1

    def _cascade_exposure(self) -> dict[str, float]:
        """Normalised downstream-failure count, per entity. Feeds RiskScorer."""
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
    async def _maybe_generate_interventions(self, node_state: dict[str, dict], sim_time: str) -> list[dict]:
        store = self.store
        # Trigger: any entity predicted critical within the hour.
        triggers = [
            (eid, f) for eid, f in store.forecasts.items()
            if f.get("time_to_critical_sec") is not None and f["time_to_critical_sec"] <= 3600
        ]
        if not triggers:
            return []
        triggers.sort(key=lambda t: t[1]["time_to_critical_sec"])
        root = triggers[0][0]

        # Do not re-propose for a root that already has a live proposal, and keep
        # the operator queue to a size a human can actually read during an
        # incident. An unbounded queue is not more information, it is less.
        proposed = [i for i in store.interventions.values() if i["status"] == "proposed"]
        if any(i["triggered_by_entity_id"] == root for i in proposed):
            return []
        if len(proposed) >= MAX_LIVE_PROPOSALS:
            return []

        risk_context = {
            "root_entity_id": root,
            "cascade": store.cascades.get(root),
            "node_state": node_state,
            "edges": store.edges,
            "sim_time": sim_time,
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

        for candidate in candidates:
            certificate, degraded, certify_ms = await call_ml(
                "equilibrium.certify",
                self.certify_candidate,
                self.registry.equilibrium.fallback,
                self.config.budget_sec("certify"),
                candidate,
                node_state,
            )
            candidate["certificate"] = certificate
            candidate["created_at"] = sim_time
            candidate["expires_at"] = shift(sim_time, candidate.pop("_ttl_sec", 900))

        # rank() attaches rank_score using the certificate verdict — this is the
        # step where an UNSTABLE high-relief option loses to a STABLE lower one.
        ranked = self.registry.optimiser.rank(candidates)

        for i in ranked:
            store.interventions[i["intervention_id"]] = i
            cert = i.get("certificate")
            if cert:
                store.certificates[i["intervention_id"]] = cert

        self._count_unstable_caught(ranked)
        return ranked

    def _count_unstable_caught(self, ranked: list[dict]) -> None:
        """01 §3.10 — how many UNSTABLE options a relief-only ranking would have picked.

        This is the number that justifies the whole equilibrium layer, so it is
        counted precisely: only when relief-first would have chosen it and the
        certificate demoted it.
        """
        if not ranked:
            return
        by_relief = max(ranked, key=lambda i: float(i.get("estimated_relief_pct", 0.0)))
        verdict = (by_relief.get("certificate") or {}).get("verdict")
        if verdict == "UNSTABLE" and ranked[0]["intervention_id"] != by_relief["intervention_id"]:
            self.store.unstable_caught += 1

    SETTLE_DELAY_SEC = 900

    async def _settle_executing_interventions(self, sim_time: str) -> list[dict]:
        """Close the loop: an executing intervention becomes a regret-ledger entry.

        Phase 1B — matched counterfactual. At settlement (900 sim-s after
        approval) two utilisation values are read from the SAME event-world
        model at the SAME instant:

            actual          the live world (this intervention's effects included)
            counterfactual  the live world with this intervention's effects
                            excluded — every other event since approval kept

        The measured quantity is the mean utilisation of the intervention's
        SOURCE entities (the ones it is meant to relieve). All three percentages
        use that one quantity and that one horizon:

            realised_relief_pct       = (cf - actual) / cf * 100        vs do-nothing
            counterfactual_relief_pct = (u_approval - cf) / u_approval * 100
                                        (how the source would have moved anyway)
            regret                    = predicted_relief_pct - realised_relief_pct

        No clamping: the old +/-100 clamp hid a twin branch predicting 915%
        utilisation. If a denominator is ~0 the value is unavailable (null).
        """
        store = self.store
        settled = []
        for i in list(store.interventions.values()):
            if i["status"] != "executing":
                continue
            applied = i.get("_applied_at")
            if not applied or (parse(sim_time) - parse(applied)).total_seconds() < self.SETTLE_DELAY_SEC:
                continue

            sources = [s for s in i.get("_sources") or i["target_entity_ids"] if s in store.nodes]
            effect_ids = tuple(i.get("_effect_ids") or ())
            actual_world = self.generator.evaluate()
            cf_world = self.generator.evaluate(exclude=effect_ids)

            def mean_util(world: dict[str, dict]) -> float | None:
                vals = [world[s]["utilisation"] for s in sources if s in world]
                return sum(vals) / len(vals) if vals else None

            actual = mean_util(actual_world)
            counterfactual = mean_util(cf_world)
            at_approval = i.get("_source_util_at_approval")
            predicted = float(i["estimated_relief_pct"])

            realised = (
                round((counterfactual - actual) / counterfactual * 100.0, 1)
                if counterfactual is not None and actual is not None and counterfactual > 0.01 else None
            )
            cf_change = (
                round((at_approval - counterfactual) / at_approval * 100.0, 1)
                if at_approval and counterfactual is not None and at_approval > 0.01 else None
            )

            # Decision-panel evidence: affected entities, actual vs matched do-nothing.
            affected = list(dict.fromkeys(sources + [d for d in i.get("_destinations") or [] if d in store.nodes]))
            store.settlements.append({
                "intervention_id": i["intervention_id"],
                "affected": affected,
                "actual_peak": max((actual_world[e]["utilisation"] for e in affected), default=0.0),
                "counterfactual_peak": max((cf_world[e]["utilisation"] for e in affected), default=0.0),
                "actual_zone_variance": variance([actual_world[z]["utilisation"] for z in store.zone_ids()]),
                "counterfactual_zone_variance": variance([cf_world[z]["utilisation"] for z in store.zone_ids()]),
            })

            predicted_util = i.get("_predicted_source_util_at_settle")
            if predicted_util is not None and actual is not None:
                store.certificates_scored.append(abs(predicted_util - actual) <= max(0.15 * actual, 0.05))

            i["status"] = "completed"
            entry = {
                "regret_id": f"reg_{len(store.regret_entries) + 1:04d}",
                "intervention_id": i["intervention_id"],
                "intervention_type": i["intervention_type"],
                "predicted_relief_pct": predicted,
                "realised_relief_pct": realised,
                "counterfactual_relief_pct": cf_change,
                "regret": None if realised is None else round(predicted - realised, 2),
                "sim_time": sim_time,
            }
            store.regret_entries.append(entry)
            settled.append(entry)
        return settled

    # --- step 2 -------------------------------------------------------------------------------------
    def _persist(self, sim_time: str) -> None:
        from ..db.base import SessionLocal
        from ..db import models

        ts = parse(sim_time)
        store = self.store
        try:
            with SessionLocal() as session:
                session.bulk_save_objects(
                    [
                        models.EntityState(
                            entity_id=s["entity_id"], sim_time=ts,
                            current_count=s["current_count"], utilisation=s["utilisation"],
                            flow_rate_per_min=s["flow_rate_per_min"], risk_score=s["risk_score"],
                            risk_band=s["risk_band"], is_observed=s["is_observed"],
                        )
                        for s in store.entity_states.values()
                    ]
                )
                session.bulk_save_objects(
                    [
                        models.Forecast(
                            entity_id=eid, generated_at=ts, source=f["source"],
                            horizon_sec=p["horizon_sec"],
                            predicted_utilisation=p["predicted_utilisation"],
                            lower_90=p.get("lower_90"), upper_90=p.get("upper_90"),
                            time_to_critical_sec=f.get("time_to_critical_sec"),
                        )
                        for eid, f in store.forecasts.items()
                        for p in f["points"]
                    ]
                )

                # Interventions/certificates/nudges/regret entries are mutable
                # (status flips, a certificate arrives after the intervention
                # row does) so they're upserted by primary key every cycle
                # rather than bulk-inserted — these tables were entirely
                # write-never before this: every decision the demo makes was
                # visible only in the in-memory StateStore and vanished on
                # restart, despite 01 §1 naming "audit logging and the regret
                # ledger" as something the backend owns.
                for i in store.interventions.values():
                    session.merge(
                        models.Intervention(
                            intervention_id=i["intervention_id"], intervention_type=i["intervention_type"],
                            status=i["status"], target_entity_ids=i["target_entity_ids"],
                            triggered_by_entity_id=i.get("triggered_by_entity_id"),
                            title=i["title"], description=i["description"],
                            estimated_relief_pct=i["estimated_relief_pct"],
                            estimated_cost_paise=i["estimated_cost_paise"],
                            estimated_delay_sec=i["estimated_delay_sec"], feasibility=i["feasibility"],
                            rank_score=i["rank_score"], created_at=parse(i["created_at"]),
                            expires_at=parse(i["expires_at"]),
                        )
                    )
                    cert = store.certificates.get(i["intervention_id"])
                    if cert:
                        session.merge(
                            models.Certificate(
                                certificate_id=cert["certificate_id"], intervention_id=i["intervention_id"],
                                verdict=cert["verdict"], converged=cert["converged"],
                                iterations=cert["iterations"], post_nudge_variance=cert["post_nudge_variance"],
                                baseline_variance=cert["baseline_variance"],
                                max_zone_utilisation=cert["max_zone_utilisation"],
                                max_zone_entity_id=cert.get("max_zone_entity_id"),
                                oscillation_risk=cert["oscillation_risk"],
                                compliance_sensitivity=cert["compliance_sensitivity"],
                                compliance_sweep=cert["compliance_sweep"], reason=cert["reason"],
                            )
                        )

                for entry in store.regret_entries:
                    session.merge(
                        models.RegretEntry(
                            regret_id=entry["regret_id"], intervention_id=entry["intervention_id"],
                            intervention_type=entry["intervention_type"],
                            predicted_relief_pct=entry["predicted_relief_pct"],
                            realised_relief_pct=entry["realised_relief_pct"],
                            counterfactual_relief_pct=entry["counterfactual_relief_pct"],
                            regret=entry["regret"], sim_time=parse(entry["sim_time"]),
                        )
                    )

                for n in store.nudges.values():
                    session.merge(
                        models.Nudge(
                            nudge_id=n["nudge_id"], attendee_id=n["attendee_id"],
                            intervention_id=n.get("intervention_id"), segment_id=n.get("segment_id"),
                            headline=n["headline"], body=n["body"], tradeoff=n["tradeoff"],
                            target_entity_id=n.get("target_entity_id"), status=n["status"],
                            issued_at=parse(n["issued_at"]), expires_at=parse(n["expires_at"]),
                        )
                    )

                session.commit()
        except Exception:
            # Persistence is the durable record, not the live path. Losing a write
            # must not take the demo down with it.
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
            {"cycle_number": store.cycle_number, "sim_time": sim_time, "summary": store.summary},
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
            await MANAGER.broadcast("intervention_queued", {"intervention": i}, sim_time)

        for i in expired:
            await MANAGER.broadcast(
                "intervention_resolved",
                {"intervention_id": i["intervention_id"], "status": "expired"},
                sim_time,
            )

        # An executing intervention that has now settled (>=900s after approval)
        # produces a regret-ledger entry. 01 §4.1 lists `regret_update` as a live
        # event but `_broadcast` never emitted it and the return value of
        # `_settle_executing_interventions` was discarded — so the realised-vs-
        # counterfactual result of an operator's decision only ever surfaced on
        # a reconnect. `intervention_resolved{status:completed}` is emitted
        # alongside so the operator queue can reconcile the card off `executing`.
        if settled:
            from .metrics import build_regret

            summary = build_regret(self)["summary"]
            for entry in settled:
                await MANAGER.broadcast(
                    "intervention_resolved",
                    {"intervention_id": entry["intervention_id"], "status": "completed"},
                    sim_time,
                )
                await MANAGER.broadcast(
                    "regret_update", {"entry": entry, "summary": summary}, sim_time
                )

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
            # §2: shed the forecast, never the assimilation.
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
        forecasts = [
            f for eid, f in store.forecasts.items()
            if entity_ids is None or eid in entity_ids
        ]
        return {
            "generated_at": store.sim_time,
            "active_source": store.active_forecast_source,
            "forecasts": forecasts,
        }

    def cascade_payload(self) -> dict[str, Any]:
        store = self.store
        return {
            "sim_time": store.sim_time,
            "source": store.active_cascade_source,
            "cascades": list(store.cascades.values()),
        }

    def last_cycle_ms(self) -> float:
        return self._last_cycle_ms

    # --- demo control (01 §3.13) --------------------------------------------------------------------------------
    async def demo_control(
        self,
        action: str | None,
        seed: int | None,
        speed_multiplier: float | None,
        seek_to_sim_time: str | None,
        inject: dict | None,
        auto_pause_on_intervention: bool | None = None,
    ) -> dict[str, Any]:
        if seed is not None:
            self.seed = seed
        if speed_multiplier is not None:
            self.speed_multiplier = validated_speed(speed_multiplier)
        if auto_pause_on_intervention is not None:
            self.auto_pause_on_intervention = bool(auto_pause_on_intervention)

        if action == "pause":
            self.paused = True
            self.pause_reason = {"kind": "operator", "cycle_number": self.store.cycle_number,
                                 "sim_time": self.store.sim_time}
        elif action == "play":
            self.paused = False
            self.pause_reason = None
        elif action == "set_speed":
            pass
        elif action == "reset":
            async with self._cycle_lock:
                await self._reset()
            await self.broadcast_resync()
        elif action == "seek" and seek_to_sim_time:
            elapsed = (parse(seek_to_sim_time) - parse(self.store.sim_start)).total_seconds()
            self.generator.seek(max(0.0, elapsed))
            self.store.reset_clock(seek_to_sim_time)
        elif action == "step":
            # Exactly one real cycle, then stay paused. Serialised with the loop
            # so it can never interleave with a cycle already in flight.
            self.paused = True
            async with self._cycle_lock:
                self.pause_reason = {"kind": "step"}
                await self.run_cycle()
            if self.pause_reason and self.pause_reason.get("kind") == "step":
                self.pause_reason = {"kind": "step", "cycle_number": self.store.cycle_number,
                                     "sim_time": self.store.sim_time}
        elif action == "next_decision":
            # Run at the current speed until the optimiser queues its next real
            # proposal batch, then pause (see run_cycle step 11). No synthetic
            # events: if nothing is proposed, the sim simply keeps running.
            self._pause_on_next_decision = True
            self.paused = False
            self.pause_reason = None

        if inject:
            self.generator.inject(inject["scenario_type"], inject.get("params", {}))

        await self.broadcast_demo_status()
        return self.demo_status()

    # --- intervention execution ----------------------------------------------------------------
    def execute_intervention(self, item: dict) -> dict:
        """Apply an approved intervention to the live world (Phase 1C).

        Each `action_effects` entry becomes a conserved generator transfer:
            moved = planned_fraction x response x source people (ramping in over ramp_sec)
            source -= moved;  destination += moved   (destination None = deferral)
        `response` is the share of the offered move that attendees actually take:
        the certificate's equilibrium response rate when certified, otherwise the
        nominal segment-weighted compliance (see `nominal_response`).

        Interventions with no modelled crowd effect (notify_only,
        emergency_corridor) do NOT touch demand — before Phase 1 approving
        "notify operations team" cut real demand by 4%.

        Records what the matched counterfactual needs: effect ids, sources,
        destinations and the sources' utilisation at approval.
        """
        effects = item.get("action_effects") or []
        response = self.response_rate(item)
        effect_ids: list[str] = []
        for eff in effects:
            src, dst = eff["source_entity_id"], eff.get("destination_entity_id")
            if src not in self.store.nodes or (dst is not None and dst not in self.store.nodes):
                continue
            effect_ids.append(self.generator.add_transfer(
                src, dst, float(eff["planned_fraction"]) * response,
                ramp_sec=float(eff.get("ramp_sec") or 0),
                duration_sec=eff.get("duration_sec"),
                tag=item["intervention_id"],
            ))
        sources = list(dict.fromkeys(e["source_entity_id"] for e in effects if e["source_entity_id"] in self.store.nodes))
        if not sources:
            # No crowd effect: measure at the entity it was raised for.
            root = item.get("triggered_by_entity_id") or (item["target_entity_ids"] or [None])[0]
            sources = [root] if root in self.store.nodes else []
        destinations = list(dict.fromkeys(
            e["destination_entity_id"] for e in effects
            if e.get("destination_entity_id") in self.store.nodes
        ))
        truth = self.generator.evaluate()
        # 03 §5.6 certificate accuracy: what the certified model predicts the
        # sources' utilisation will be at settlement, compared there with reality.
        # (Same event-world model, so this can only diverge through events that
        # happen after approval — other approvals, injected disruptions.)
        if sources and effects:
            predicted_world = self.world_rollout(self.generator)
            moved = [{**e, "fraction": float(e["planned_fraction"]) * response} for e in effects]
            projection = predicted_world(moved, sources, self.SETTLE_DELAY_SEC)
            item["_predicted_source_util_at_settle"] = sum(projection[s][-1] for s in sources) / len(sources)
        item["_effect_ids"] = effect_ids
        item["_sources"] = sources
        item["_destinations"] = destinations
        item["_response_rate"] = response
        item["_source_util_at_approval"] = (
            sum(truth[s]["utilisation"] for s in sources) / len(sources) if sources else None
        )
        self._branch_seq = getattr(self, "_branch_seq", 0) + 1
        return {"branch_id": f"sim_{self._branch_seq:04x}", "effect_ids": effect_ids}

    # Share of an offered move that attendees take, at the nominal (0.6) row of
    # the compliance sweep with no cost advantage: sum(share x base_rate) x 0.6.
    NOMINAL_COMPLIANCE_ROW = 0.6

    def nominal_response(self) -> float:
        return sum(
            float(s["share"]) * float(s["compliance_base_rate"]) for s in self.store.segments
        ) * self.NOMINAL_COMPLIANCE_ROW

    def response_rate(self, item: dict) -> float:
        cert = item.get("certificate") or {}
        rate = cert.get("response_rate")
        return float(rate) if rate is not None else self.nominal_response()

    # --- certification hook ------------------------------------------------------------------
    ROLLOUT_STEP_SEC = 30

    def world_rollout(self, world: Any = None):
        """A rollout over a frozen clone of the event-world model (Phase 1D).

        `rollout(moved, entities, horizon_sec)` applies `moved` transfers to a
        fresh copy of that clone and returns each entity's utilisation at every
        30-sim-second step. The live generator is never touched, so certificates,
        what-if and settlement all use the same model the simulation runs."""
        frozen = (world or self.generator).clone()
        start = frozen.elapsed_sec()

        def rollout(moved: list[dict], entities: list[str], horizon_sec: int) -> dict[str, list[float]]:
            branch = frozen.clone()
            for e in moved:
                branch.add_transfer(
                    e["source_entity_id"], e.get("destination_entity_id"), float(e["fraction"]),
                    ramp_sec=float(e.get("ramp_sec") or 0), duration_sec=e.get("duration_sec"),
                )
            steps = max(1, int(horizon_sec) // self.ROLLOUT_STEP_SEC)
            frames = [branch.evaluate(start + self.ROLLOUT_STEP_SEC * (k + 1), only=entities) for k in range(steps)]
            return {e: [f[e]["utilisation"] for f in frames] for e in entities}

        return rollout

    def certify_candidate(self, candidate: dict, node_state: dict[str, dict], world: Any = None) -> dict:
        """Certify one candidate against the event-world model (live cycle and what-if)."""
        return self.registry.equilibrium.certify(
            candidate, node_state, self.store.edges, self.store.segments, rollout=self.world_rollout(world)
        )

    # --- observer status (Phase 0) ------------------------------------------------------
    def demo_status(self) -> dict[str, Any]:
        return {
            "status": "paused" if self.paused else "playing",
            "sim_time": self.store.sim_time,
            "seed": self.seed,
            "speed_multiplier": self.speed_multiplier,
            "cycle_number": self.store.cycle_number,
            "run_id": self.run_id,
            "cycle_sec": int(self.sim_dt),
            "wall_seconds_per_cycle": round(self.wall_seconds_per_cycle(), 3),
            "auto_pause_on_intervention": self.auto_pause_on_intervention,
            "pause_on_next_decision": self._pause_on_next_decision,
            "pause_reason": self.pause_reason,
        }

    async def broadcast_demo_status(self) -> None:
        await MANAGER.broadcast("demo_status", self.demo_status(), self.store.sim_time)

    def resync_payload(self) -> dict[str, Any]:
        """Full state for a (re)connecting client — never a delta (01 §4)."""
        from .metrics import build_regret

        store = self.store
        return {
            "state": self.state_payload(),
            "pressure_timeline": store.pressure_timeline,
            "active_forecast_source": store.active_forecast_source,
            "interventions": [
                {k: v for k, v in i.items() if not k.startswith("_")}
                for i in store.interventions_by_status("proposed", limit=10)
            ],
            "cascades": list(store.cascades.values()),
            "twin_fidelity": store.twin_fidelity_payload(),
            "regret": build_regret(self),
            "nudges": list(store.nudges.values()),
            "demo": self.demo_status(),
        }

    async def broadcast_resync(self) -> None:
        """After a reset every open client must drop the previous timeline."""
        await MANAGER.broadcast("resync", self.resync_payload(), self.store.sim_time)

    async def _reset(self) -> None:
        """Full reproducible reset — the H33 rehearsal depends on this being exact."""
        self.store.clear_live()
        self.store.reset_clock(self.store.sim_start)
        self.generator.reset(self.seed)
        self.registry = MLRegistry()
        self.generator = self.registry.build_generator(
            {"nodes": list(self.store.nodes.values()), "edges": self.store.edges}, self.seed
        )
        self._init_twin()
        self.store.drift_mode_enabled = False
        CACHE.clear()
        self.run_id += 1
        self.pause_reason = None
        self._pause_on_next_decision = False
        commander = getattr(self, "commander", None)
        if commander is not None:
            commander.clear_cache()

        # The clock rewinds to sim_start, so persistence would otherwise
        # re-insert entity_state rows keyed on sim_times this same process just
        # wrote before the reset — a guaranteed UNIQUE-constraint failure on
        # (entity_id, sim_time) from cycle 1 onward. Clear the DB-backed time
        # series so "reproduces the identical run twice" (01 §8) holds at the
        # persistence layer too, not just in memory.
        from ..db.seed import clear_run_tables

        await asyncio.to_thread(clear_run_tables)
        log.info("demo reset to seed %s", self.seed)

    def set_drift_mode(self, enabled: bool) -> dict[str, Any]:
        self.store.drift_mode_enabled = enabled
        started = self.store.sim_time if enabled else None
        if hasattr(self.registry.twin, "set_drift_mode"):
            self.registry.twin.set_drift_mode(enabled, started)
        if enabled:
            # Both series restart from the same point, so the divergence reads clean.
            self.store.twin_history.clear()
        return {"drift_mode_enabled": enabled, "uncorrected_ensemble_started_at": started}


def validated_speed(value: Any) -> float:
    """Sim-seconds per wall-second: finite and > 0. The API rejects anything
    else with a 400 before it gets here; this guards config and direct callers."""
    speed = float(value)
    if not math.isfinite(speed) or speed <= 0:
        raise ValueError(f"speed_multiplier must be a finite number > 0, got {value!r}")
    return speed


ENGINE: Engine | None = None


def get_engine() -> Engine:
    if ENGINE is None:  # pragma: no cover - guarded by app lifespan
        raise RuntimeError("engine not started")
    return ENGINE


def set_engine(engine: Engine | None) -> None:
    global ENGINE
    ENGINE = engine
