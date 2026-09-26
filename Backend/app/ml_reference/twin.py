"""AssimilatedTwin — Ensemble Kalman Filter (03_ML_CONTRACT.md §3).

State (per ensemble member, 2N rows, unchanged from the contract):
    rows 0..N-1   count_i   people present at entity i            [people]
    rows N..2N-1  flow_i    net rate of change of count_i         [people / minute]

Observation operator H selects the count rows of observed entities; each
observation is a (simulated) sensor count.

Phase 1A rewrite. FINAL_AUDIT_REPORT P0-01 measured unobserved entities pinned
at 0.0 / 2.0 utilisation and flows of up to 13x capacity per minute. Three
mechanisms were isolated experimentally (PHASE1_VALIDATION_REPORT.md §1A):

  1. The Kalman gain was unlocalised. Members carried INDEPENDENT noise per
     entity, so every cross-entity covariance was pure sampling noise, and 53
     observations pushed spurious increments into every unobserved row.
  2. After the analysis step, each observed flow was overwritten with
     2 x (analysis increment), so the next forecast step re-applied the same
     increment: a positive-feedback loop that grew flows without bound.
  3. Multiplicative inflation (x1.05 per step) was applied to rows that no
     observation ever corrects, so their spread grew exponentially and the
     count >= 0 clip dragged the mean to the bounds.

What replaces them:

  * OBSERVED entities — a genuine stochastic EnKF, LOCALISED to the entity
    itself: an observation updates only its own entity's count and flow rows.
    With no physical coupling between entities in the surrogate model, any
    cross-entity gain would be sampling noise (mechanism 1). No flow
    overwrite (mechanism 2). Inflation is applied only to these rows, which
    the analysis step actually corrects every cycle (mechanism 3).
  * UNOBSERVED entities — a documented, deterministic PEER-TREND estimate
    instead of an uncorrected, freely integrating ensemble:
        u_i(t) = u_i(t0) + median_{j in observed peers of the same type}( u_j(t) - u_j(t0) )
    Its uncertainty is the dispersion of those peers' changes (std, floored),
    realised as fixed per-member offsets, so ensemble_std for an unobserved
    entity reflects how much its type's members actually disagree. With no
    observed peer, the entity persists at its last estimate with a floor
    spread. (An experiment that instead shared a type-level flow component
    through the ensemble drifted: unobserved flows absorbed peers'
    innovations every cycle but never received a count correction to cancel
    the overshoot — PHASE1_VALIDATION_REPORT.md §1A.)
  * Observation error scales with the reading (1% relative, floor
    `obs_noise_var`), because sensor error on a 70,000-seat venue is not 5
    people.
  * Guards: non-finite members are replaced from the last good ensemble;
    counts are clipped to [0, U_MAX x capacity]; flows to +/-FLOW_MAX; spread
    is capped.

The t0 anchor is the generator's opening state (a pre-event survey), as
before this change.

`branch()` never touches the live ensemble OR the live RNG (it uses a private
generator seeded from (seed, branch number)), and applies a scenario's
demand multipliers ONCE at the start of the horizon, not every step.
"""
from __future__ import annotations

import logging
from typing import Any

import numpy as np

log = logging.getLogger("eventflow.ml.twin")

# Utilisation upper bound for the STATE (not the display). Over-capacity is
# meaningful, so this is well above 1.0; it only stops numerical runaway.
U_MAX = 2.0
# Flow bound: |flow| <= this many capacities per minute. The synthetic world's
# steepest real ramp is ~0.03 capacity/min; 0.25 leaves ample headroom.
FLOW_MAX_CAP_PER_MIN = 0.25
# Unobserved spread cap (utilisation std). Without an observation to correct
# it, spread would grow for ever; beyond this it carries no information.
SPREAD_CAP_UTIL = 0.30
# Relative sensor error used for R (documented in the synthetic generator as
# +/-1.5% uniform => std ~0.87%; 1% is a conservative round figure).
REL_OBS_ERROR = 0.01
# Flow persistence per 30 s step (as before) and process-noise scales, in
# utilisation units per minute per step.
#
# Calibrated (PHASE1_VALIDATION_REPORT.md §1A) on seed 42 and checked on held-out
# seeds 7 and 123: SIGMA_COUNT_UTIL 0.012 brings the observed entities' 90%
# coverage from ~0.63 to ~0.91 AND lowers their error (the old 0.001 made the
# prior over-confident, so the filter lagged every ramp); PEER_SPREAD_SCALE only
# widens unobserved uncertainty (0.83-0.98 coverage) and never moves the mean.
FLOW_DECAY = 0.92
SIGMA_FLOW = 0.0010
SIGMA_COUNT_UTIL = 0.012
# Peer-trend uncertainty (utilisation std) for unobserved entities.
PEER_SPREAD_FLOOR = 0.03
PEER_SPREAD_SCALE = 1.5
NO_PEER_SPREAD = 0.08


class AssimilatedTwin:
    def __init__(self, config: dict) -> None:
        self.config = config or {}
        self.m = int(self.config.get("ensemble_size", 20))
        self.inflation = float(self.config.get("inflation_factor", 1.05))
        self.obs_noise_var = float(self.config.get("obs_noise_var", 25.0))
        self.process_noise_var = float(self.config.get("process_noise_var", 9.0))
        self.drift_mode_enabled = bool(self.config.get("drift_mode_enabled", False))
        self.seed = int(self.config.get("seed", 42))

        self.entity_ids: list[str] = []
        self.entity_types: list[str] = []
        self.capacities: np.ndarray = np.zeros(0)
        self._type_index: np.ndarray = np.zeros(0, dtype=int)
        self._n_types = 0
        self._X: np.ndarray = np.zeros((0, 0))          # corrected ensemble, (2N, m)
        self._X_uncorrected: np.ndarray | None = None    # drift-mode open-loop copy
        self._last_good: np.ndarray | None = None
        self._observed_rows: np.ndarray = np.zeros(0, dtype=bool)
        self._rng = np.random.default_rng(self.seed)
        self._last_fidelity: dict[str, Any] | None = None
        self._drift_started_at: str | None = None
        self._branch_counter = 0
        self.guard_events = 0
        # Known control inputs (Phase 1C): transfers the operator approved and
        # the system executed, as (source, destination|None, active fraction).
        self._known_transfers: list[tuple[str, str | None, float]] = []

    # --- setup --------------------------------------------------------------
    def initialise(
        self,
        entity_ids: list[str],
        capacities: dict[str, float],
        counts: dict[str, float],
        entity_types: dict[str, str] | None = None,
    ) -> None:
        self.entity_ids = list(entity_ids)
        n = len(self.entity_ids)
        self.capacities = np.array([max(1.0, float(capacities[e])) for e in self.entity_ids], dtype=float)
        types = entity_types or {}
        self.entity_types = [types.get(e, "unknown") for e in self.entity_ids]
        order = sorted(set(self.entity_types))
        self._type_index = np.array([order.index(t) for t in self.entity_types], dtype=int)
        self._n_types = len(order)
        self._observed_rows = np.zeros(n, dtype=bool)

        base = np.array([counts.get(e, 0.0) for e in self.entity_ids], dtype=float)
        self._rng = np.random.default_rng(self.seed)
        spread = np.maximum(base * 0.05, 0.01 * self.capacities)
        members = base[:, None] + self._rng.normal(0.0, 1.0, size=(n, self.m)) * spread[:, None]
        flows = self._rng.normal(0.0, 1.0, size=(n, self.m)) * (0.002 * self.capacities)[:, None]
        self._X = self._bound(np.vstack([members, flows]))
        self._X_uncorrected = None
        self._last_good = self._X.copy()
        # Peer-trend anchors (utilisation at t0) and fixed standard-normal
        # member offsets, so an unobserved entity's ensemble is smooth and
        # deterministic under the seed.
        self._anchor_util = base / self.capacities
        z = self._rng.normal(0.0, 1.0, size=(n, self.m))
        self._member_z = (z - z.mean(axis=1, keepdims=True)) / np.maximum(z.std(axis=1, keepdims=True), 1e-9)

    def ready(self) -> bool:
        return self._X.size > 0

    @property
    def n(self) -> int:
        return len(self.entity_ids)

    # --- bounds / guards -------------------------------------------------------
    def _bound(self, X: np.ndarray) -> np.ndarray:
        n = self.n
        caps = self.capacities[:, None]
        X[:n] = np.clip(X[:n], 0.0, U_MAX * caps)
        X[n:] = np.clip(X[n:], -FLOW_MAX_CAP_PER_MIN * caps, FLOW_MAX_CAP_PER_MIN * caps)
        return X

    def _guard(self, X: np.ndarray) -> np.ndarray:
        """Replace non-finite members; cap spread of rows no observation constrains."""
        if not np.isfinite(X).all():
            self.guard_events += 1
            log.warning("twin: non-finite ensemble values replaced from last good state")
            bad = ~np.isfinite(X)
            fallback = self._last_good if self._last_good is not None else np.zeros_like(X)
            X[bad] = fallback[bad]
        n = self.n
        counts = X[:n]
        mean = counts.mean(axis=1, keepdims=True)
        std = counts.std(axis=1, keepdims=True)
        cap_std = SPREAD_CAP_UTIL * self.capacities[:, None]
        scale = np.where(std > cap_std, cap_std / np.maximum(std, 1e-12), 1.0)
        X[:n] = mean + (counts - mean) * scale
        return self._bound(X)

    # --- 03 §3.1 forecast step ----------------------------------------------
    def step(self, dt_sec: int = 30) -> None:
        if not self.ready():
            return
        self._X = self._advance(self._X, dt_sec, self._rng, inflate=True)
        if self._X_uncorrected is not None:
            # Open-loop copy: same model, same noise process, never assimilated
            # and never inflated (inflation exists only to balance the analysis).
            self._X_uncorrected = self._advance(self._X_uncorrected, dt_sec, self._rng, inflate=False)

    def _advance(self, X: np.ndarray, dt_sec: int, rng: np.random.Generator, inflate: bool) -> np.ndarray:
        n = self.n
        X = X.copy()
        counts, flows = X[:n], X[n:]
        minutes = dt_sec / 60.0
        caps = self.capacities[:, None]

        new_counts = counts + flows * minutes
        new_flows = FLOW_DECAY * flows + rng.normal(0.0, SIGMA_FLOW, size=flows.shape) * caps
        count_sigma = np.maximum(np.sqrt(self.process_noise_var), SIGMA_COUNT_UTIL * caps)
        new_counts = new_counts + rng.normal(0.0, 1.0, size=counts.shape) * count_sigma

        X = np.vstack([new_counts, new_flows])
        if inflate and self._observed_rows.any():
            # §3.2 inflation, applied only where observations constrain the state.
            rows = np.concatenate([self._observed_rows, self._observed_rows])
            mean = X[rows].mean(axis=1, keepdims=True)
            X[rows] = mean + self.inflation * (X[rows] - mean)
        return self._peer_trend(self._guard(X))

    def set_known_transfers(self, transfers: list[tuple[str, str | None, float]]) -> None:
        """Executed interventions are KNOWN inputs, not something to infer: an
        unobserved source/destination of an approved transfer is adjusted by
        the people it moved. Observed entities need nothing — their sensors
        already see the result."""
        self._known_transfers = [(s, d, float(f)) for s, d, f in transfers if f > 0]

    def _apply_known_transfers(self, X: np.ndarray) -> np.ndarray:
        if not self._known_transfers or not self._observed_rows.size:
            return X
        n = self.n
        index = {e: i for i, e in enumerate(self.entity_ids)}
        out_frac: dict[int, float] = {}
        for src, _, f in self._known_transfers:
            if src in index:
                out_frac[index[src]] = out_frac.get(index[src], 0.0) + f
        # Pre-transfer people at each source: an observed source is already
        # post-transfer (measured), so undo its known outflow; an unobserved
        # source's peer-trend estimate is pre-transfer.
        pre = {}
        for i, f in out_frac.items():
            f = min(f, 0.999)
            pre[i] = X[i] / (1.0 - f) if self._observed_rows[i] else X[i].copy()
        delta = np.zeros_like(X[:n])
        for src, dst, f in self._known_transfers:
            if src not in index:
                continue
            i = index[src]
            moved = pre[i] * f / max(out_frac[i], 1.0)
            if not self._observed_rows[i]:
                delta[i] -= moved
            if dst is not None and dst in index and not self._observed_rows[index[dst]]:
                delta[index[dst]] += moved
        X[:n] = np.clip(X[:n] + delta, 0.0, U_MAX * self.capacities[:, None])
        return X

    # --- unobserved entities: peer-trend estimate --------------------------------------
    def _peer_trend(self, X: np.ndarray) -> np.ndarray:
        """Overwrite unobserved rows with the documented peer-trend estimate."""
        n = self.n
        observed = self._observed_rows
        if observed.size == 0 or not observed.any():
            return X  # nothing observed yet: the forecast step is all we have
        caps = self.capacities
        util_mean = X[:n].mean(axis=1) / caps
        flow_util = X[n:].mean(axis=1) / caps
        delta = util_mean - self._anchor_util
        for i in np.flatnonzero(~observed):
            peers = np.flatnonzero(observed & (self._type_index == self._type_index[i]))
            if peers.size:
                centre = float(np.median(delta[peers]))
                disp = float(np.std(delta[peers], ddof=1)) if peers.size >= 2 else abs(centre) * 0.25
                spread = max(disp * PEER_SPREAD_SCALE, PEER_SPREAD_FLOOR)
                flow = float(np.median(flow_util[peers])) * caps[i]
                level = self._anchor_util[i] + centre
            else:
                spread = NO_PEER_SPREAD
                flow = 0.0
                level = util_mean[i]
            members = np.clip(level + spread * self._member_z[i], 0.0, U_MAX) * caps[i]
            X[i] = members
            X[n + i] = flow
        return self._apply_known_transfers(X)

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
        n = self.n
        index = {e: i for i, e in enumerate(self.entity_ids)}
        obs_ids = [e for e in observations if e in index and np.isfinite(observations[e])]
        self._observed_rows = np.zeros(n, dtype=bool)

        if obs_ids:
            rows = np.array([index[e] for e in obs_ids])
            self._observed_rows[rows] = True
            y = np.array([max(0.0, float(observations[e])) for e in obs_ids])
            k = len(rows)
            r_var = np.maximum(self.obs_noise_var, (REL_OBS_ERROR * y) ** 2)

            X = self._X
            HX = X[rows, :]
            A = X - X.mean(axis=1, keepdims=True)
            HA = A[rows, :]
            denom = max(self.m - 1, 1)

            # Localisation (Schur product): observation j updates ONLY the count
            # and flow rows of its own entity. The surrogate has no physical
            # coupling between entities, so any cross-entity gain would be
            # sampling noise from a 20-member ensemble (mechanism 1).
            same_entity = (np.arange(n)[:, None] == rows[None, :]).astype(float)  # (N, k)
            L_state = np.vstack([same_entity, same_entity])                        # (2N, k)
            L_obs = np.eye(k)
            PHt = (A @ HA.T) / denom * L_state
            HPHt = (HA @ HA.T) / denom * L_obs
            S = HPHt + np.diag(r_var)
            try:
                K = np.linalg.solve(S.T, PHt.T).T
            except np.linalg.LinAlgError:
                log.warning("HPH^T + R singular; using pseudo-inverse")
                K = PHt @ np.linalg.pinv(S)

            perturbed = y[:, None] + self._rng.normal(0.0, 1.0, size=(k, self.m)) * np.sqrt(r_var)[:, None]
            X = X + K @ (perturbed - HX)
            self._X = self._peer_trend(self._guard(X))
            self._last_good = self._X.copy()

        return self._fidelity(observations, sim_time, truth)

    def _rmse(self, ensemble: np.ndarray | None, reference: dict[str, float]) -> float | None:
        if ensemble is None or not reference:
            return None
        index = {e: i for i, e in enumerate(self.entity_ids)}
        keys = [e for e in reference if e in index]
        if not keys:
            return None
        rows = [index[e] for e in keys]
        estimate = ensemble[:self.n].mean(axis=1)[rows]
        actual = np.array([reference[e] for e in keys], dtype=float)
        return float(np.sqrt(np.mean((estimate - actual) ** 2)))

    def _coverage(self, reference: dict[str, float] | None) -> float | None:
        """Fraction of entities whose reference count lies inside the ensemble's
        90% interval (mean +/- 1.645 sd). Measured, not assumed (03 §3.6)."""
        if not reference:
            return None
        index = {e: i for i, e in enumerate(self.entity_ids)}
        keys = [e for e in reference if e in index]
        if not keys:
            return None
        rows = [index[e] for e in keys]
        counts = self._X[:self.n][rows]
        mean, sd = counts.mean(axis=1), counts.std(axis=1, ddof=1)
        actual = np.array([reference[e] for e in keys], dtype=float)
        inside = np.abs(actual - mean) <= 1.645 * np.maximum(sd, 1e-9)
        return round(float(inside.mean()), 3)

    def _fidelity(self, observations: dict[str, float], sim_time: str | None,
                  truth: dict[str, float] | None) -> dict:
        reference = truth or observations
        assimilated = self._rmse(self._X, reference) or 0.0
        uncorrected = self._rmse(self._X_uncorrected, reference) if self._X_uncorrected is not None else None

        # Honest comparison: the uncorrected copy runs the SAME bounded model
        # open-loop from the moment drift mode was enabled, so this is "what
        # assimilation adds over the model alone" — never a diverging strawman.
        improvement = None
        if uncorrected is not None and uncorrected > 1e-9:
            improvement = round((uncorrected - assimilated) / uncorrected * 100.0, 1)

        counts = self._X[:self.n]
        spread = float(np.mean(counts.std(axis=1) / np.maximum(self.capacities, 1.0)))

        fidelity = {
            "sim_time": sim_time,
            "assimilated_rmse": round(assimilated, 2),
            "uncorrected_rmse": None if uncorrected is None else round(uncorrected, 2),
            "improvement_pct": improvement,
            "ensemble_size": self.m,
            "ensemble_spread": round(spread, 4),
            "ensemble_coverage": self._coverage(truth) if truth else None,
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
            "ensemble_coverage": None,
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
                "utilisation": float(mean_counts[i] / cap),
                "flow_rate_per_min": float(mean_flows[i]),
                "is_observed": bool(self._observed_rows[i]) if self._observed_rows.size else False,
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
        """Fork the ensemble, apply a scenario, roll forward.

        Never mutates live state and never consumes the live RNG: the branch
        uses its own generator seeded from (seed, branch number), so a
        what-if or approval can no longer perturb the live filter's noise.
        """
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
        # Private RNGs: identical noise for baseline and scenario (common
        # random numbers), independent of the live filter's RNG.
        seed_seq = [self.seed, self._branch_counter]

        cap_mult = np.ones(self.n)
        demand_mult = np.ones(self.n)
        index = {e: i for i, e in enumerate(self.entity_ids)}
        for eid, mult in (scenario.get("capacity_multipliers") or {}).items():
            if eid in index:
                cap_mult[index[eid]] = float(mult)
        for eid, mult in (scenario.get("demand_multipliers") or {}).items():
            if eid in index:
                demand_mult[index[eid]] = float(mult)

        base_X = self._X.copy()
        scen_X = self._X.copy()
        # Demand multipliers apply ONCE (to people present and to their flow),
        # not compounded every step.
        scen_X[:self.n] *= demand_mult[:, None]
        scen_X[self.n:] *= demand_mult[:, None]
        rng_base = np.random.default_rng(seed_seq)
        rng_scen = np.random.default_rng(seed_seq)

        traj: dict[str, list[float]] = {e: [] for e in self.entity_ids}
        for _ in range(steps):
            base_X = self._advance(base_X, dt, rng_base, inflate=False)
            scen_X = self._advance(scen_X, dt, rng_scen, inflate=False)
            util = scen_X[:self.n].mean(axis=1) / np.maximum(self.capacities * cap_mult, 1.0)
            for i, eid in enumerate(self.entity_ids):
                traj[eid].append(round(float(util[i]), 4))

        base_util = base_X[:self.n].mean(axis=1) / np.maximum(self.capacities, 1.0)
        scen_util = scen_X[:self.n].mean(axis=1) / np.maximum(self.capacities * cap_mult, 1.0)
        new_critical = [
            self.entity_ids[i]
            for i in range(self.n)
            if scen_util[i] >= 0.90 > base_util[i]
        ]
        return {
            "branch_id": branch_id,
            "baseline": self._side(base_util),
            "scenario": self._side(scen_util),
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
