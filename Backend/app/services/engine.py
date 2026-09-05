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
# Operator queue ceiling — the frontend shows 10; beyond that it is noise.
MAX_LIVE_PROPOSALS = 8


class Engine:
    """Owns the clock, the ML registry, and the only writer to StateStore."""

    def __init__(self) -> None:
        self.config = get_config()
        event_cfg = self.config.raw["event"]

        self.seed = self.config.demo_seed
        self.speed_multiplier = float(self.config.raw.get("speed_multiplier", 60))
        self.sim_dt = self.config.cycle_sec
        self.paused = False

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

    # --- lifecycle ------------------------------------------------------------
    def _init_twin(self) -> None:
        caps = self.store.capacities()
        truth = self.generator.ground_truth()
        counts = {e: v["current_count"] for e, v in truth.items()}
        if hasattr(self.registry.twin, "initialise"):
            self.registry.twin.initialise(list(self.store.nodes.keys()), caps, counts)

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
        await self._settle_executing_interventions(sim_time)

        # 2. persist ------------------------------------------------------------------
        await asyncio.to_thread(self._persist, sim_time)
        self._write_cache()

        # 9. broadcast -------------------------------------------------------------------
        await self._broadcast(previous_states, fidelity, anomalies or [], new_cascades,
                              new_interventions, expired, sim_time)

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
        store = self.store
        for eid, node in store.nodes.items():
            cap = float(node["nominal_capacity"]) or 1.0
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
            candidate["expires_at"] = shift(sim_time, candidate.pop("_ttl_sec", 900))
            await self._score_certificate_accuracy(candidate, certificate, node_state)

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

    async def _score_certificate_accuracy(
        self, candidate: dict, certificate: dict, node_state: dict[str, dict]
    ) -> None:
        """03 §5.6 — compare the certificate's predicted equilibrium against an
        independent `twin.branch()` rollout of the same relief. Agreement within
        15% is recorded; this is the only place `certificates_scored` is written,
        and `metrics.certificate_accuracy_pct` is the only place it is read.

        Deliberately a *different* mechanism from `certify()`'s own best-response
        solver (a flat demand cut on the named targets vs. a compliance-weighted
        segment model) — the point of this check is cross-validation, not
        re-deriving the same number twice.
        """
        if not certificate.get("converged"):
            return  # no equilibrium prediction to compare against
        targets = candidate.get("target_entity_ids") or []
        relief = float(candidate.get("estimated_relief_pct", 0.0)) / 100.0
        scenario = {"demand_multipliers": {t: clamp(1.0 - relief, 0.05, 1.0) for t in targets if t in node_state}}
        if not scenario["demand_multipliers"]:
            return
        branch, degraded, _ = await call_ml(
            "twin.branch_check", self.registry.twin.branch, None, 0.2, scenario, 1800,
        )
        if degraded or not branch:
            return
        predicted = float(certificate.get("max_zone_utilisation", 0.0))
        observed = float((branch.get("scenario") or {}).get("peak_utilisation", 0.0))
        within_15pct = abs(predicted - observed) <= max(0.15 * observed, 0.05)
        self.store.certificates_scored.append(within_15pct)

    # `_counterfactual_trajectory` is a 6-point rollout at 300s increments
    # (see AssimilatedTwin._branch, horizon_sec=1800 // 6 steps); settling at
    # exactly 900s after approval lands on index 2.
    SETTLE_DELAY_SEC = 900
    _COUNTERFACTUAL_STEP_SEC = 300

    async def _settle_executing_interventions(self, sim_time: str) -> list[dict]:
        """Close the loop: an executing intervention becomes a regret-ledger entry.

        Both `realised_relief_pct` and `counterfactual_relief_pct` are measured
        against the same baseline (`_util_at_approval`) and the same settlement
        point — one from what the live simulation, with the relief actually
        applied, shows now; the other from the do-nothing branch forked at
        approval time. Neither is derived from `hash()` (which also broke
        seed-42 reproducibility per 01 §8 — a per-process-salted hash of the
        intervention id is not a function of the seed at all).
        """
        store = self.store
        settled = []
        for i in list(store.interventions.values()):
            if i["status"] != "executing":
                continue
            applied = i.get("_applied_at")
            if not applied or (parse(sim_time) - parse(applied)).total_seconds() < self.SETTLE_DELAY_SEC:
                continue

            predicted = float(i["estimated_relief_pct"])
            baseline = float(i.get("_util_at_approval", 0.0))
            targets = [t for t in i["target_entity_ids"] if t in store.entity_states]
            traj = i.get("_counterfactual_trajectory") or {}
            idx = self.SETTLE_DELAY_SEC // self._COUNTERFACTUAL_STEP_SEC - 1

            actual_now = (
                sum(store.entity_states[t]["utilisation"] for t in targets) / len(targets)
                if targets else baseline
            )
            do_nothing_now = (
                sum(traj[t][idx] for t in targets if t in traj and len(traj[t]) > idx)
                / max(sum(1 for t in targets if t in traj and len(traj[t]) > idx), 1)
                if any(t in traj and len(traj[t]) > idx for t in targets) else baseline
            )

            # A near-zero baseline turns a small absolute swing into a huge
            # percentage (the twin's branch model is a crude ABM surrogate —
            # see twin.py — not the generator's true curve, so it can diverge
            # from what actually happens by more than a percentage swing
            # should reasonably report). Clamped to the same +/-100 scale
            # `estimated_relief_pct` itself uses, so an outlier reads as "very
            # wrong" rather than as a plausible-looking three-digit number.
            if baseline > 0.05:
                realised = round(clamp((baseline - actual_now) / baseline * 100.0, -100.0, 100.0), 1)
                counterfactual = round(clamp((baseline - do_nothing_now) / baseline * 100.0, -100.0, 100.0), 1)
            else:
                realised = 0.0
                counterfactual = 0.0

            for t in targets:
                if t in traj and len(traj[t]) > idx:
                    store.counterfactual_utilisation[t] = float(traj[t][idx])

            i["status"] = "completed"
            entry = {
                "regret_id": f"reg_{len(store.regret_entries) + 1:04d}",
                "intervention_id": i["intervention_id"],
                "intervention_type": i["intervention_type"],
                "predicted_relief_pct": predicted,
                "realised_relief_pct": realised,
                "counterfactual_relief_pct": counterfactual,
                "regret": round(predicted - realised, 2),
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
    ) -> dict[str, Any]:
        if seed is not None:
            self.seed = seed
        if speed_multiplier is not None:
            self.speed_multiplier = float(speed_multiplier)

        if action == "pause":
            self.paused = True
        elif action == "play":
            self.paused = False
        elif action == "set_speed":
            pass
        elif action == "reset":
            await self._reset()
        elif action == "seek" and seek_to_sim_time:
            elapsed = (parse(seek_to_sim_time) - parse(self.store.sim_start)).total_seconds()
            self.generator.seek(max(0.0, elapsed))
            self.store.reset_clock(seek_to_sim_time)

        if inject:
            self.generator.inject(inject["scenario_type"], inject.get("params", {}))

        return {
            "status": "paused" if self.paused else "playing",
            "sim_time": self.store.sim_time,
            "seed": self.seed,
            "speed_multiplier": self.speed_multiplier,
        }

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


ENGINE: Engine | None = None


def get_engine() -> Engine:
    if ENGINE is None:  # pragma: no cover - guarded by app lifespan
        raise RuntimeError("engine not started")
    return ENGINE


def set_engine(engine: Engine | None) -> None:
    global ENGINE
    ENGINE = engine
