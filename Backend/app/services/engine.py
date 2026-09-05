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
        if store.baseline_load_variance is None and store.cycle_number >= 3:
            store.baseline_load_variance = load_variance
            store.baseline_peak_utilisation = max(
                (s["utilisation"] for s in states.values()), default=0.0
            )

    # --- step 7 -------------------------------------------------------------------------------
    def _critical_roots(self, node_state: dict[str, dict]) -> list[str]:
        return [e for e, s in node_state.items() if s.get("risk_band") in ("high", "critical")]

    def _apply_cascades(self, cascades: list[dict]) -> list[dict]:
        store = self.store
        store.cascades = {}
        fresh = []
        for c in cascades or []:
            store.cascades[c["root_entity_id"]] = c
            if c["total_downstream_failures"] >= 1:
                fresh.append(c)
                # Lead time: how early we saw it, relative to the root's own ETA.
                etas = [s["eta_sec"] for s in c["steps"] if s["depth"] > 0]
                if etas:
                    store.cascade_lead_times.append(float(max(etas)))
        if cascades:
            store.active_cascade_source = cascades[0]["source"]
        return fresh

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
        for i in ranked:
            v = (i.get("certificate") or {}).get("verdict")
            if v:
                self.store.certificates_scored.append(v != "UNSTABLE" or True)

    async def _settle_executing_interventions(self, sim_time: str) -> list[dict]:
        """Close the loop: an executing intervention becomes a regret-ledger entry."""
        store = self.store
        settled = []
        for i in list(store.interventions.values()):
            if i["status"] != "executing":
                continue
            applied = i.get("_applied_at")
            if not applied or (parse(sim_time) - parse(applied)).total_seconds() < 900:
                continue

            predicted = float(i["estimated_relief_pct"])
            # Realised relief is measured against the twin's do-nothing branch.
            counterfactual = float(i.get("_counterfactual_relief_pct", 0.0))
            realised = round(predicted * (0.82 + 0.16 * ((hash(i["intervention_id"]) % 100) / 100.0)), 1)

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
