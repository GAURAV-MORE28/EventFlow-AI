"""AssimilatedTwin — Ensemble Kalman Filter (03_ML_CONTRACT.md §3).

Aggregate state only: per-entity count and flow, 2N dimensions (§3.2 is explicit
that per-agent assimilation will not finish in time and adds nothing).

Covariance inflation is mandatory, not optional. Without it the ensemble spread
collapses inside ~15 cycles and the filter silently stops correcting — the
failure mode that makes the drift-mode demo fall flat. `inflation_factor` is
applied on every forecast step.
"""
from __future__ import annotations

import logging
from typing import Any

import numpy as np

from .common import clamp

log = logging.getLogger("eventflow.ml.twin")


class AssimilatedTwin:
    def __init__(self, config: dict) -> None:
        self.config = config or {}
        self.m = int(self.config.get("ensemble_size", 20))
        self.inflation = float(self.config.get("inflation_factor", 1.05))
        self.obs_noise_var = float(self.config.get("obs_noise_var", 25.0))
        self.process_noise_var = float(self.config.get("process_noise_var", 9.0))
        self.drift_mode_enabled = bool(self.config.get("drift_mode_enabled", False))
        self.seed = int(self.config.get("seed", 42))
        # Relative noise models (fractions of capacity / of the reading).
        self.process_noise_rel = float(self.config.get("process_noise_rel", 0.004))
        self.obs_noise_rel = float(self.config.get("obs_noise_rel", 0.012))
        self.relaxation = float(self.config.get("relaxation", 0.05))

        self.entity_ids: list[str] = []
        self.capacities: np.ndarray = np.zeros(0)
        self._X: np.ndarray = np.zeros((0, 0))          # corrected ensemble, (2N, m)
        self._X_uncorrected: np.ndarray | None = None    # drift-mode comparison copy
        self._rng = np.random.default_rng(self.seed)
        self._last_fidelity: dict[str, Any] | None = None
        self._drift_started_at: str | None = None
        self._branch_counter = 0
        self._unobserved_mask: np.ndarray = np.zeros(0)
        self.last_coverage: float | None = None

    # --- setup --------------------------------------------------------------
    def initialise(self, entity_ids: list[str], capacities: dict[str, float], counts: dict[str, float]) -> None:
        self.entity_ids = list(entity_ids)
        n = len(self.entity_ids)
        self.capacities = np.array([max(1.0, capacities[e]) for e in self.entity_ids], dtype=float)
        base = np.array([counts.get(e, 0.0) for e in self.entity_ids], dtype=float)

        self._rng = np.random.default_rng(self.seed)
        spread = np.maximum(base * 0.05, 5.0)
        members = base[:, None] + self._rng.normal(0.0, spread[:, None], size=(n, self.m))
        flows = np.zeros((n, self.m))
        self._X = np.vstack([np.maximum(members, 0.0), flows])
        self._X_uncorrected = None
        self._unobserved_mask = np.zeros(n)

    def ready(self) -> bool:
        return self._X.size > 0

    @property
    def n(self) -> int:
        return len(self.entity_ids)

    # --- 03 §3.1 forecast step ----------------------------------------------
    def step(self, dt_sec: int = 30, model: dict | None = None) -> None:
        """Forecast step. `model` carries the process model's view of the world:
        `{"counts": {eid: count}, "delta": {eid: change this step}}` from the
        nominal (announced-schedule) city model. Without it the ensemble just
        persists its last state — never a free random walk.
        """
        if not self.ready():
            return
        self._X = self._advance(self._X, dt_sec, model, inflate=True)
        if self._X_uncorrected is not None:
            # Stepped but never assimilated — that divergence is the demo.
            self._X_uncorrected = self._advance(self._X_uncorrected, dt_sec, model, inflate=True)

    def shift(self, delta: dict[str, float]) -> None:
        """An announced change of the process model at the current instant (an
        operator re-planned the schedule): every member moves by the model's
        change, so estimates follow the plan without waiting for sensors."""
        if not self.ready():
            return
        v = self._vector(delta)
        if v is None:
            return
        n = self.n
        self._X[:n] = np.maximum(self._X[:n] + v[:, None], 0.0)

    def _vector(self, values: dict[str, float] | None) -> np.ndarray | None:
        if not values:
            return None
        return np.array([float(values.get(e, 0.0)) for e in self.entity_ids], dtype=float)

    def _advance(self, X: np.ndarray, dt_sec: int, model: dict | None, inflate: bool) -> np.ndarray:
        n = self.n
        counts = X[:n].copy()
        delta = self._vector((model or {}).get("delta"))
        target = self._vector((model or {}).get("counts"))
        minutes = max(dt_sec / 60.0, 1e-6)

        if delta is not None:
            counts = counts + delta[:, None]
            flows = np.repeat((delta / minutes)[:, None], self.m, axis=1)
        else:
            flows = X[n:] * 0.5  # no model: hold the level, let the trend fade
        # Weak relaxation toward the process model for entities no sensor sees
        # this cycle: bounded error instead of an unbounded random walk (the
        # root cause of the 0%/200% phantom values).
        if target is not None:
            counts += self.relaxation * (target[:, None] - counts) * self._unobserved_mask[:, None]

        # Process noise is proportional to capacity — an absolute noise of a
        # few people is meaningless against counts in the thousands and made
        # the filter trust a model that was never right.
        sd = self.process_noise_rel * self.capacities
        counts += self._rng.normal(0.0, 1.0, size=counts.shape) * sd[:, None]
        flows += self._rng.normal(0.0, 1.0, size=flows.shape) * (sd / minutes)[:, None] * 0.25
        counts = np.clip(counts, 0.0, (self.capacities * 2.5)[:, None])

        X = np.vstack([counts, flows])
        if inflate:
            mean = X.mean(axis=1, keepdims=True)
            X = mean + self.inflation * (X - mean)   # §3.2: mandatory, do not remove
            X[:n] = np.clip(X[:n], 0.0, (self.capacities * 2.5)[:, None])
        return X

    # --- 03 §3.1 analysis step ------------------------------------------------
    def assimilate(self, observations: dict[str, float], sim_time: str | None = None,
                   truth: dict[str, float] | None = None) -> dict:
        if not self.ready():
            return self.fallback(observations, sim_time)
        try:
            return self._assimilate(observations, sim_time, truth)
        except Exception:
            log.exception("assimilation failed")
            return self.fallback(observations, sim_time)

    def _assimilate(self, observations: dict[str, float], sim_time: str | None,
                    truth: dict[str, float] | None) -> dict:
        """Localised EnKF analysis: each reading corrects its own entity (count
        and flow). Cross-entity gain from a 20-member ensemble is sampling noise,
        and it is what used to drag unobserved entities to 0 or 200%. An entity
        without a reading this cycle keeps its forecast — last valid state plus
        the process model's tendency."""
        n = self.n
        index = {e: i for i, e in enumerate(self.entity_ids)}
        mask = np.ones(n)
        denom = max(self.m - 1, 1)
        # Canonical entity order, never the observation dict's order: the RNG draws
        # per reading, so an order that depends on per-process string hashing made
        # two processes with the same seed diverge.
        for e in self.entity_ids:
            if e not in observations:
                continue
            y = observations[e]
            i = index[e]
            if not np.isfinite(y):
                continue
            mask[i] = 0.0
            xi = self._X[i]
            fi = self._X[n + i]
            a = xi - xi.mean()
            var = float(a @ a) / denom
            r = self.obs_noise_var + (self.obs_noise_rel * max(float(y), 0.0)) ** 2
            if var + r <= 0:
                continue
            k = var / (var + r)
            kf = float((fi - fi.mean()) @ a) / denom / (var + r)
            perturbed = y + self._rng.normal(0.0, r ** 0.5, size=self.m)
            innov = perturbed - xi
            self._X[i] = np.clip(xi + k * innov, 0.0, self.capacities[i] * 2.5)
            self._X[n + i] = fi + kf * innov
        self._unobserved_mask = mask
        return self._fidelity(observations, sim_time, truth)

    def _rmse(self, ensemble: np.ndarray | None, reference: dict[str, float]) -> float | None:
        if ensemble is None or not reference:
            return None
        index = {e: i for i, e in enumerate(self.entity_ids)}
        rows = [index[e] for e in reference if e in index]
        if not rows:
            return None
        estimate = ensemble[:self.n].mean(axis=1)[rows]
        actual = np.array([reference[e] for e in reference if e in index], dtype=float)
        return float(np.sqrt(np.mean((estimate - actual) ** 2)))

    def _fidelity(self, observations: dict[str, float], sim_time: str | None,
                  truth: dict[str, float] | None) -> dict:
        reference = truth or observations
        assimilated = self._rmse(self._X, reference) or 0.0
        uncorrected = self._rmse(self._X_uncorrected, reference) if self._X_uncorrected is not None else None

        improvement = None
        if uncorrected is not None and uncorrected > 1e-9:
            improvement = round((uncorrected - assimilated) / uncorrected * 100.0, 1)

        counts = self._X[:self.n]
        spread = float(np.mean(counts.std(axis=1) / np.maximum(self.capacities, 1.0)))
        # Measured 90% interval coverage: how often truth falls inside mean ± 1.645σ
        # (floored at the observation-noise scale so a tight ensemble is not
        # penalised for sensor noise it cannot see).
        if truth:
            index = {e: i for i, e in enumerate(self.entity_ids)}
            hits, total = 0, 0
            mean, sd = counts.mean(axis=1), counts.std(axis=1)
            for e, v in truth.items():
                i = index.get(e)
                if i is None:
                    continue
                half = 1.645 * max(sd[i], self.obs_noise_rel * max(float(v), 1.0))
                hits += int(abs(mean[i] - float(v)) <= half)
                total += 1
            self.last_coverage = round(hits / total, 3) if total else None

        fidelity = {
            "sim_time": sim_time,
            "assimilated_rmse": round(assimilated, 2),
            "uncorrected_rmse": None if uncorrected is None else round(uncorrected, 2),
            "improvement_pct": improvement,
            "ensemble_size": self.m,
            "ensemble_spread": round(spread, 4),
            "drift_mode_enabled": self.drift_mode_enabled,
        }
        self._last_fidelity = fidelity
        return fidelity

    def fallback(self, observations: dict[str, float], sim_time: str | None = None) -> dict:
        """Last-known fidelity, improvement zeroed. A fallback is a normal state."""
        if self._last_fidelity:
            out = dict(self._last_fidelity)
            out["sim_time"] = sim_time
            out["improvement_pct"] = 0.0
            return out
        return {
            "sim_time": sim_time,
            "assimilated_rmse": 0.0,
            "uncorrected_rmse": None,
            "improvement_pct": 0.0,
            "ensemble_size": self.m,
            "ensemble_spread": 0.0,
            "drift_mode_enabled": self.drift_mode_enabled,
        }

    # --- 03 §3.1 state ---------------------------------------------------------
    def state(self) -> dict[str, dict]:
        if not self.ready():
            return {}
        counts = self._X[:self.n]
        flows = self._X[self.n:]
        mean_counts = counts.mean(axis=1)
        mean_flows = flows.mean(axis=1)
        std_counts = counts.std(axis=1)
        out: dict[str, dict] = {}
        for i, eid in enumerate(self.entity_ids):
            cap = self.capacities[i]
            out[eid] = {
                "current_count": float(mean_counts[i]),
                "utilisation": float(clamp(mean_counts[i] / cap, 0.0, 2.0)),
                "flow_rate_per_min": float(mean_flows[i]),
                "is_observed": False,
                "ensemble_std": float(std_counts[i]),
            }
        return out

    # --- 03 §3.3 drift mode -----------------------------------------------------
    def set_drift_mode(self, enabled: bool, started_at: str | None = None) -> None:
        self.drift_mode_enabled = enabled
        if enabled:
            # Reset the uncorrected copy to the current corrected state so the
            # divergence starts from zero — that is what makes it legible in 5 cycles.
            self._X_uncorrected = self._X.copy()
            self._drift_started_at = started_at
        else:
            self._X_uncorrected = None
            self._drift_started_at = None

    def drift_started_at(self) -> str | None:
        return self._drift_started_at

    # --- 03 §3.5 branch ----------------------------------------------------------
    def branch(self, scenario: dict, horizon_sec: int = 3600) -> dict:
        """Fork the ensemble, apply a scenario, roll forward. Never mutates live state."""
        try:
            return self._branch(scenario, horizon_sec)
        except Exception:
            log.exception("twin branch failed")
            self._branch_counter += 1
            return {
                "branch_id": f"sim_{self._branch_counter:04x}",
                "baseline": {"peak_utilisation": 0.0, "peak_entity_id": None,
                             "load_variance": 0.0, "critical_count": 0},
                "scenario": {"peak_utilisation": 0.0, "peak_entity_id": None,
                             "load_variance": 0.0, "critical_count": 0},
                "new_critical_entities": [],
                "trajectory": {},
            }

    def _branch(self, scenario: dict, horizon_sec: int) -> dict:
        self._branch_counter += 1
        branch_id = f"sim_{self._branch_counter:04x}"
        steps = 6
        dt = max(1, horizon_sec // steps)

        base_X = self._X.copy()
        scen_X = self._X.copy()

        # A scenario is expressed as per-entity capacity and demand multipliers.
        cap_mult = np.ones(self.n)
        demand_mult = np.ones(self.n)
        index = {e: i for i, e in enumerate(self.entity_ids)}
        for eid, mult in (scenario.get("capacity_multipliers") or {}).items():
            if eid in index:
                cap_mult[index[eid]] = float(mult)
        for eid, mult in (scenario.get("demand_multipliers") or {}).items():
            if eid in index:
                demand_mult[index[eid]] = float(mult)

        traj: dict[str, list[float]] = {e: [] for e in self.entity_ids}
        scen_X[:self.n] *= demand_mult[:, None]  # applied once, never compounded
        for _ in range(steps):
            base_X = self._advance(base_X, dt, None, inflate=False)
            scen_X = self._advance(scen_X, dt, None, inflate=False)
            util = (scen_X[:self.n].mean(axis=1) / np.maximum(self.capacities * cap_mult, 1.0))
            for i, eid in enumerate(self.entity_ids):
                traj[eid].append(round(float(util[i]), 4))

        base_util = base_X[:self.n].mean(axis=1) / np.maximum(self.capacities, 1.0)
        scen_util = scen_X[:self.n].mean(axis=1) / np.maximum(self.capacities * cap_mult, 1.0)

        baseline = self._side(base_util)
        scenario_side = self._side(scen_util)
        new_critical = [
            self.entity_ids[i]
            for i in range(self.n)
            if scen_util[i] >= 0.90 > base_util[i]
        ]

        return {
            "branch_id": branch_id,
            "baseline": baseline,
            "scenario": scenario_side,
            "new_critical_entities": new_critical,
            "trajectory": traj,
        }

    def _side(self, util: np.ndarray) -> dict:
        if util.size == 0:
            return {"peak_utilisation": 0.0, "peak_entity_id": None, "load_variance": 0.0, "critical_count": 0}
        peak_i = int(np.argmax(util))
        return {
            "peak_utilisation": round(float(util[peak_i]), 4),
            "peak_entity_id": self.entity_ids[peak_i],
            "load_variance": round(float(np.var(util)), 4),
            "critical_count": int(np.sum(util >= 0.90)),
        }
