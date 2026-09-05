"""SyntheticGenerator (03_ML_CONTRACT.md §8).

The generator is the ground truth: it is the demo environment, the evaluation
baseline for the twin, and the pressure source for the cascade. It is fully
determined by `seed`, so the H33 rehearsal ("seed 42, three times, identical")
holds.

Shape of the world: every entity has an occupancy curve

    utilisation(t) = base + amplitude * logistic((t - t_mid) / tau) + exit_spike(t)

with per-entity constants derived from a hash of the entity id, so they are
stable across runs and independent of call order. `metro_b` is tuned by hand so
its forecast reads ~18 minutes to critical about twelve cycles in — that is the
demo's hero number and it is not left to chance.
"""
from __future__ import annotations

import math
from typing import Any

from .common import clamp, stable_unit

# metro_b: solved so u(6 min) ~= 0.61 and u(24 min) ~= 0.90 -> the 18-minute headline.
HERO_ENTITY = "metro_b"
HERO_PROFILE = {"base": 0.30, "amp": 0.95, "t_mid_min": 16.3, "tau_min": 14.2, "noise": 0.008}

# Per-type shape. Transport leads, gates follow, roads and emergency lag —
# which is what makes the propagation order physically legible on the map.
TYPE_PROFILE: dict[str, dict[str, float]] = {
    "transport_node":    {"base": 0.22, "amp": 0.70, "t_mid_min": 22.0, "tau_min": 16.0, "noise": 0.015},
    "transport_route":   {"base": 0.28, "amp": 0.60, "t_mid_min": 20.0, "tau_min": 15.0, "noise": 0.012},
    "gate":              {"base": 0.14, "amp": 0.72, "t_mid_min": 30.0, "tau_min": 17.0, "noise": 0.018},
    "venue":             {"base": 0.08, "amp": 0.85, "t_mid_min": 38.0, "tau_min": 20.0, "noise": 0.010},
    "zone":              {"base": 0.20, "amp": 0.58, "t_mid_min": 28.0, "tau_min": 18.0, "noise": 0.020},
    "road":              {"base": 0.26, "amp": 0.55, "t_mid_min": 34.0, "tau_min": 19.0, "noise": 0.022},
    "parking":           {"base": 0.18, "amp": 0.66, "t_mid_min": 26.0, "tau_min": 18.0, "noise": 0.016},
    "hotel":             {"base": 0.52, "amp": 0.22, "t_mid_min": 24.0, "tau_min": 22.0, "noise": 0.008},
    # Emergency posts are small and already busy during a mega-event; that is
    # precisely why a modest downstream surge tips them. Modelling them as near-
    # idle would make the last link of the cascade chain unreachable.
    "emergency_facility": {"base": 0.30, "amp": 0.42, "t_mid_min": 34.0, "tau_min": 18.0, "noise": 0.014},
}

# Fraction of entities carrying a live sensor. The rest are twin estimates and
# the frontend draws them with a dashed outline (02 §5.2).
OBSERVED_FRACTION = 0.75

# The demo chain and the saturation target are the instrumented assets — a real
# operator puts sensors on the stations and gates that matter, and the demo needs
# these four drawn solid rather than dashed (00 §5).
ALWAYS_OBSERVED = {"metro_b", "gate_3", "road_4", "emergency_north", "gate_5", "metro_c"}


def _logistic(x: float) -> float:
    if x < -60:
        return 0.0
    if x > 60:
        return 1.0
    return 1.0 / (1.0 + math.exp(-x))


class SyntheticGenerator:
    def __init__(self, config: dict, topology: dict, seed: int = 42) -> None:
        self.config = config or {}
        self.seed = seed
        self.topology = topology
        self.nodes: dict[str, dict[str, Any]] = {n["entity_id"]: n for n in topology["nodes"]}

        self._elapsed_sec = 0.0
        self._prev_counts: dict[str, float] = {}
        # Capacity and intensity multipliers mutated by inject(); persist until reset.
        self._capacity_mult: dict[str, float] = {}
        self._intensity_mult: dict[str, float] = {}
        self._global_intensity = 1.0
        self._closed: set[str] = set()
        self._injected: list[dict[str, Any]] = []

        self._profiles = {eid: self._profile_for(eid) for eid in self.nodes}
        self._observed = {
            eid: eid in ALWAYS_OBSERVED or stable_unit(seed, "observed", eid) < OBSERVED_FRACTION
            for eid in self.nodes
        }
        self._seed_initial_counts()

    # --- 03 §0 universal surface ----------------------------------------
    def ready(self) -> bool:
        return True

    def fallback(self, dt_sec: int = 30) -> dict[str, float]:
        """Hold the last observation. A frozen world beats a blank one."""
        return dict(self._prev_counts)

    # --- setup -----------------------------------------------------------
    def _profile_for(self, entity_id: str) -> dict[str, float]:
        if entity_id == HERO_ENTITY:
            return dict(HERO_PROFILE)
        node = self.nodes[entity_id]
        base = dict(TYPE_PROFILE.get(node["entity_type"], TYPE_PROFILE["zone"]))
        # Spread entities widely around their type's shape. A narrow spread makes
        # every zone sit at the same utilisation, which collapses `load_variance`
        # to ~0.000 — and load variance is the PS-8 objective the demo has to
        # show dropping. It needs room to move.
        jitter = stable_unit(self.seed, "profile", entity_id)
        # Zones carry the widest spread of all: their utilisation *is* the
        # load-variance signal, so they must start genuinely unevenly loaded.
        span = 2.10 if node["entity_type"] == "zone" else 1.20
        base["base"] = clamp(base["base"] * (0.40 + span * jitter), 0.02, 0.95)
        base["amp"] = base["amp"] * (0.55 + 0.90 * stable_unit(self.seed, "amp", entity_id))
        base["t_mid_min"] += (jitter - 0.5) * 10.0
        base["tau_min"] *= 0.85 + 0.3 * stable_unit(self.seed, "tau", entity_id)
        return base

    def _seed_initial_counts(self) -> None:
        for eid in self.nodes:
            self._prev_counts[eid] = self._true_count(eid, 0.0)

    def capacity(self, entity_id: str) -> float:
        cap = float(self.nodes[entity_id]["nominal_capacity"])
        return max(1.0, cap * self._capacity_mult.get(entity_id, 1.0))

    # --- the curve --------------------------------------------------------
    def _true_utilisation(self, entity_id: str, elapsed_sec: float) -> float:
        if entity_id in self._closed:
            return 0.0
        p = self._profiles[entity_id]
        minutes = elapsed_sec / 60.0
        ramp = _logistic((minutes - p["t_mid_min"]) / max(p["tau_min"], 1e-6))
        util = p["base"] + p["amp"] * ramp

        # Post-event exit spike (03 §8.2) — the second pressure source.
        exit_start = float(self.config.get("exit_spike_start_min", 150.0))
        if minutes > exit_start and self.nodes[entity_id]["entity_type"] in (
            "transport_node", "gate", "road", "transport_route"
        ):
            util += 0.45 * _logistic((minutes - exit_start - 8.0) / 6.0)

        util *= self._global_intensity * self._intensity_mult.get(entity_id, 1.0)

        # Deterministic wobble: a function of (entity, time bucket), never of an RNG
        # cursor, so replaying the same seed reproduces the run exactly.
        bucket = int(elapsed_sec // 30)
        noise = (stable_unit(self.seed, entity_id, bucket) - 0.5) * 2.0 * p["noise"]
        return clamp(util + noise, 0.0, 1.6)

    def _true_count(self, entity_id: str, elapsed_sec: float) -> float:
        return self._true_utilisation(entity_id, elapsed_sec) * self.capacity(entity_id)

    # --- 03 §8.1 interface -------------------------------------------------
    def tick(self, dt_sec: int = 30) -> dict[str, float]:
        """Advance the world and return observed counts (observed entities only)."""
        self._elapsed_sec += dt_sec
        observations: dict[str, float] = {}
        for eid in self.nodes:
            true_count = self._true_count(eid, self._elapsed_sec)
            self._prev_counts[eid] = true_count
            if self._observed[eid]:
                # Sensor noise: ±1.5%, deterministic.
                bucket = int(self._elapsed_sec // 30)
                err = (stable_unit(self.seed, "sensor", eid, bucket) - 0.5) * 0.03
                observations[eid] = max(0.0, true_count * (1.0 + err))
        return observations

    def ground_truth(self) -> dict[str, dict[str, Any]]:
        """Full true state. Evaluation only — never exposed through the API."""
        out: dict[str, dict[str, Any]] = {}
        prev_sec = max(0.0, self._elapsed_sec - 30.0)
        for eid in self.nodes:
            count = self._true_count(eid, self._elapsed_sec)
            prev = self._true_count(eid, prev_sec)
            cap = self.capacity(eid)
            out[eid] = {
                "current_count": count,
                "utilisation": count / cap,
                "flow_rate_per_min": (count - prev) / 0.5,  # 30 sim-sec window
                "is_observed": self._observed[eid],
                "capacity": cap,
            }
        return out

    def elapsed_sec(self) -> float:
        return self._elapsed_sec

    # --- 03 §8.3 injectable disruptions ------------------------------------
    def inject(self, scenario_type: str, params: dict | None = None) -> None:
        p = params or {}
        self._injected.append({"scenario_type": scenario_type, "params": p})
        eid = p.get("entity_id")
        delta = float(p.get("delta_pct", 0.0)) / 100.0

        if scenario_type == "attendance_delta":
            self._global_intensity *= 1.0 + delta
        elif scenario_type in ("metro_capacity_delta", "road_capacity_delta"):
            if eid in self.nodes:
                self._capacity_mult[eid] = self._capacity_mult.get(eid, 1.0) * (1.0 + delta)
                # Riders do not evaporate: they show up somewhere downstream.
                for e in self.topology["edges"]:
                    if e["src_entity_id"] == eid and e["edge_type"] == "feeds":
                        dst = e["dst_entity_id"]
                        self._intensity_mult[dst] = self._intensity_mult.get(dst, 1.0) * (1.0 - delta * 0.6)
        elif scenario_type == "weather_rain":
            factor = {"light": 1.05, "moderate": 1.12, "heavy": 1.22}.get(str(p.get("intensity", "moderate")), 1.12)
            for nid, node in self.nodes.items():
                if node["entity_type"] in ("road", "parking", "gate"):
                    self._intensity_mult[nid] = self._intensity_mult.get(nid, 1.0) * factor
        elif scenario_type == "gate_closure":
            if eid in self.nodes:
                self._closed.add(eid)
                siblings = [
                    n for n, nd in self.nodes.items()
                    if nd["entity_type"] == "gate" and n != eid and n not in self._closed
                ]
                for s in siblings:
                    self._intensity_mult[s] = self._intensity_mult.get(s, 1.0) * (1.0 + 1.0 / max(len(siblings), 1))
        elif scenario_type == "transport_outage":
            if eid in self.nodes:
                self._closed.add(eid)
                for e in self.topology["edges"]:
                    if e["src_entity_id"] == eid and e["edge_type"] == "substitutes_for":
                        dst = e["dst_entity_id"]
                        self._intensity_mult[dst] = self._intensity_mult.get(dst, 1.0) * (1.0 + e["substitutability"])
        elif scenario_type == "parking_loss":
            if eid in self.nodes:
                self._capacity_mult[eid] = self._capacity_mult.get(eid, 1.0) * (1.0 + delta)
        elif scenario_type == "hotel_shortage":
            for nid, node in self.nodes.items():
                if node["entity_type"] == "hotel":
                    self._intensity_mult[nid] = self._intensity_mult.get(nid, 1.0) * 1.15
        elif scenario_type == "concurrent_event":
            self._global_intensity *= 1.0 + float(p.get("overlap_pct", 15.0)) / 100.0
        elif scenario_type == "combined":
            for sub in p.get("scenarios", []):
                self.inject(sub.get("scenario_type", ""), sub.get("params", {}))

    def reset(self, seed: int | None = None) -> None:
        if seed is not None:
            self.seed = seed
        self._elapsed_sec = 0.0
        self._capacity_mult.clear()
        self._intensity_mult.clear()
        self._closed.clear()
        self._injected.clear()
        self._global_intensity = 1.0
        self._profiles = {eid: self._profile_for(eid) for eid in self.nodes}
        self._observed = {
            eid: eid in ALWAYS_OBSERVED or stable_unit(self.seed, "observed", eid) < OBSERVED_FRACTION
            for eid in self.nodes
        }
        self._seed_initial_counts()

    def seek(self, elapsed_sec: float) -> None:
        self._elapsed_sec = max(0.0, elapsed_sec)
        self._seed_initial_counts()

    def injected(self) -> list[dict[str, Any]]:
        return list(self._injected)

    def generate_cascade_dataset(self, n_scenarios: int = 5000, randomise_topology: bool = True) -> list[dict]:
        """GNN training data. Stub here — owned by the ML workstream (03 §8.1).

        `randomise_topology=True` is required (03 §8.4): a model trained on one map
        learns that map, not propagation. The real implementation lives in `ml/`.
        """
        raise NotImplementedError(
            "generate_cascade_dataset belongs to the ML workstream (ml/generator.py); "
            "the backend reference generator only drives the live demo."
        )
