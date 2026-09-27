"""Weather → EventFlow state: the transparent impact layer.

This module is the only place that turns a weather reading into numbers the
simulation understands. It produces an **impact vector**: a small set of
dimensionless multipliers and offsets that the city model already knows how to
apply (`SyntheticGenerator._rebuild`, modifier kind `"weather"`). Nothing here
touches state; nothing here talks to the network.

    WeatherConditions ──► WeatherImpactModel.impact() ──► ImpactVector
                                                              │
                                     generator.inject("weather", {"impact": …})
                                                              │
                          travel time · dwell · service rate · capacity ·
                          attendance · arrival timing · emergency load
                                                              │
                          the existing flow model propagates it:
                          roads → stations → gates → venue → egress → cascades

## What kind of model this is

Every relationship below is **rule-based**: a documented coefficient applied to
a measured value. They are engineering estimates chosen to be directionally
right and monotone, not fitted to observed data, and the payload labels them
`"rule_based"` so no consumer can mistake them for a learned model. Where a
figure has a real-world basis it is cited in the rule's `basis` string; where it
does not, the basis says `"engineering estimate"`. This module makes no
scientific claim beyond "wetter/hotter/windier makes movement slower and
capacity smaller, by roughly this much".

## Response shape

Effects are normalised against a reference intensity (`rain_ref_mm_per_hr`,
`heat_ref_c`, `wind_ref_kph`) and saturate, so no weather input can drive a
multiplier to an absurd value. Every scalar carries an uncertainty band derived
from two real sources — never an invented confidence number:

* the **coefficient range** in configuration (`k_min` … `k_max`): a rule whose
  strength is not precisely known reports the span it could plausibly take;
* the **forecast horizon**: a reading `n` hours ahead widens by
  `horizon_widening_per_hour`, because a forecast is less certain than an
  observation.

`confidence` is `high` for a live observation with a narrow band, `medium` for a
near-term forecast or a cached reading, `low` beyond `low_confidence_after_sec`
or when the reading is stale. `basis` says which of the two widened the band.
"""
from __future__ import annotations

import logging
from typing import Any

from ..ml_reference.common import clamp

log = logging.getLogger("eventflow.weather.impact")

# Entity types whose capacity weather physically reduces. Types only — never
# entity ids, so a generated OSM world behaves exactly like the demo city.
RAIN_CAPACITY_TYPES = ("road", "parking")
FLOOD_CAPACITY_TYPES = ("road", "parking", "transport_node")

# Defaults for every coefficient, so the module is fully functional with an
# empty `weather:` config block. `k` is the central value; `k_min`/`k_max`
# bound it for the uncertainty band.
DEFAULTS: dict[str, Any] = {
    # Reference intensities: the value at which a rule reaches its full effect.
    "rain_ref_mm_per_hr": 25.0,     # violent rain-rate class; heavier saturates
    "heat_ref_c": 45.0,             # apparent temperature at which heat effects saturate
    "heat_onset_c": 32.0,           # below this, heat has no modelled effect
    "wind_ref_kph": 88.0,           # Beaufort 10
    "wind_onset_kph": 39.0,         # Beaufort 6
    "flood_onset": 0.05,            # flood severity below this is ignored
    # Rain → movement and capacity.
    "rain_travel":      {"k": 0.32, "k_min": 0.20, "k_max": 0.50,
                         "basis": "wet-weather urban travel-time increase, planning estimate"},
    "rain_dwell":       {"k": 0.22, "k_min": 0.12, "k_max": 0.35,
                         "basis": "shelter-seeking and slower pedestrian flow, engineering estimate"},
    "rain_service":     {"k": 0.14, "k_min": 0.08, "k_max": 0.22,
                         "basis": "slower gate scanning under umbrellas/wet tickets, engineering estimate"},
    "rain_capacity":    {"k": 0.18, "k_min": 0.10, "k_max": 0.28,
                         "basis": "reduced saturation flow on wet pavement, planning estimate"},
    # Attendance elasticity for a TICKETED event is low — a ticket holder at a
    # sold-out final comes in the rain. An earlier calibration used k=0.12, which
    # cut demand hard enough to make heavy rain look like a relief measure.
    "rain_attendance":  {"k": 0.04, "k_min": 0.01, "k_max": 0.10,
                         "basis": "weather no-shows at ticketed events are small, engineering estimate"},
    "rain_arrival_min": {"k": 12.0, "k_min": 4.0, "k_max": 22.0,
                         "basis": "later departure from origin in rain, engineering estimate"},
    # Rain-delayed arrivals BUNCH as well as shift: the decision to wait out a
    # shower correlates across the population, so the arrival window narrows and
    # the instantaneous peak rises. This is what makes rain worse rather than
    # merely later, and it is the mechanism an operator actually has to manage.
    "rain_bunching":    {"k": 0.22, "k_min": 0.10, "k_max": 0.34,
                         "basis": "correlated departure decisions narrow the arrival window, "
                                  "engineering estimate"},
    # Heat → behaviour and medical load.
    "heat_dwell":       {"k": 0.18, "k_min": 0.08, "k_max": 0.30,
                         "basis": "shade-seeking and slower movement in heat, engineering estimate"},
    "heat_attendance":  {"k": 0.04, "k_min": 0.01, "k_max": 0.10,
                         "basis": "heat no-shows at ticketed events are small, engineering estimate"},
    "heat_arrival_min": {"k": 14.0, "k_min": 5.0, "k_max": 26.0,
                         "basis": "arrival shifted away from the hottest hours, engineering estimate"},
    "heat_bunching":    {"k": 0.20, "k_min": 0.08, "k_max": 0.32,
                         "basis": "arrivals concentrate into the cooler hour before the event, "
                                  "engineering estimate"},
    "heat_emergency":   {"k": 0.85, "k_min": 0.40, "k_max": 1.60,
                         "basis": "heat-illness presentations rise with apparent temperature, engineering estimate"},
    # Wind → open-air operations.
    "wind_service":     {"k": 0.12, "k_min": 0.05, "k_max": 0.22,
                         "basis": "open-air gate and platform operations in high wind, engineering estimate"},
    "wind_capacity":    {"k": 0.10, "k_min": 0.04, "k_max": 0.18,
                         "basis": "reduced road speeds in high wind, engineering estimate"},
    # Flooding → localised access loss. Applied to exposed entity TYPES, plus
    # any entity the operator names explicitly in a what-if.
    "flood_capacity":   {"k": 0.55, "k_min": 0.35, "k_max": 0.80,
                         "basis": "carriageway/lot loss to standing water, engineering estimate"},
    "flood_travel":     {"k": 0.45, "k_min": 0.25, "k_max": 0.70,
                         "basis": "detours around impassable sections, engineering estimate"},
    "flood_closure_severity": 0.80,   # at/above this, entities named by the operator close
    # Uncertainty.
    "horizon_widening_per_hour": 0.06,
    "low_confidence_after_sec": 10800,      # 3 h out: the band is wide enough to say "low"
    "stale_confidence": "low",
    # Safety rails on the composed vector.
    "limits": {
        "travel_time_mult": [1.0, 2.5], "dwell_mult": [1.0, 2.2], "service_rate_mult": [0.4, 1.0],
        "attendance_mult": [0.5, 1.0], "emergency_gain_mult": [1.0, 3.0],
        "capacity_mult": [0.3, 1.0], "arrival_shift_min": [0.0, 90.0],
        "arrival_spread_mult": [0.6, 1.0],
    },
}

# The causal chain shown on the Digital Twin page. Each link names the EventFlow
# quantity the impact vector actually moves, so the UI is describing the code.
CAUSAL_CHAIN = [
    {"stage": "weather", "label": "Weather",
     "detail": "Measured rainfall, apparent temperature, wind; flooding as a scenario input."},
    {"stage": "behaviour", "label": "Traveller behaviour",
     "detail": "attendance_mult, arrival_shift_min, arrival_spread_mult — who comes, when, "
               "and how tightly bunched.",
     "fields": ["attendance_mult", "arrival_shift_min", "arrival_spread_mult"]},
    {"stage": "network", "label": "Network & roads",
     "detail": "travel_time_mult, capacity of roads and car parks.",
     "fields": ["travel_time_mult", "type_capacity_mult"]},
    {"stage": "access", "label": "Stations & gates",
     "detail": "service_rate_mult, dwell_mult — queues build when service slows.",
     "fields": ["service_rate_mult", "dwell_mult"]},
    {"stage": "venue", "label": "Venue & precinct",
     "detail": "Gate queues spill onto approach roads; the venue fills later and slower.",
     "fields": ["dwell_mult"]},
    {"stage": "crowd", "label": "Crowd, congestion & risk",
     "detail": "Utilisation, risk bands, cascades and emergency load, computed by the usual cycle.",
     "fields": ["emergency_gain_mult"]},
]


def _coeff(cfg: dict, name: str) -> dict[str, Any]:
    """A coefficient block, defaults filled in, so a partial config is valid."""
    base = dict(DEFAULTS[name])
    override = cfg.get(name)
    if isinstance(override, dict):
        base.update({k: v for k, v in override.items() if k in ("k", "k_min", "k_max", "basis")})
    elif override is not None:
        base["k"] = float(override)          # a bare number sets the central value
    base.setdefault("k_min", base["k"])
    base.setdefault("k_max", base["k"])
    return base


def _scalar(cfg: dict, name: str, default: float) -> float:
    value = cfg.get(name, default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _band(cfg: dict, key: str) -> tuple[float, float]:
    limits = {**DEFAULTS["limits"], **(cfg.get("limits") or {})}
    low, high = limits.get(key, DEFAULTS["limits"].get(key, [0.0, 10.0]))
    return float(low), float(high)


class WeatherImpactModel:
    """Rule-based weather → impact-vector translation. Stateless and pure:
    the same conditions always produce the same vector (requirement: the model
    must be deterministic and reproducible for identical input)."""

    #: Declared so the payload can never disagree with the code.
    model_kind = "rule_based"
    model_version = "weather-impact-1"

    def __init__(self, config: dict | None = None) -> None:
        self.cfg = dict(config or {})

    # --- normalised drivers --------------------------------------------------
    def drivers(self, conditions: dict[str, Any], flood_severity: float = 0.0) -> dict[str, float]:
        """Each weather input reduced to a 0..1 intensity. `null` (unknown) reads
        as "no effect from this driver", which is the only safe reading of an
        absent measurement — it is never treated as zero rainfall *reported*."""
        cfg = self.cfg
        rain = conditions.get("precipitation_mm_per_hr")
        apparent = conditions.get("apparent_temp_c")
        if apparent is None:
            apparent = conditions.get("temp_c")
        wind = conditions.get("wind_kph")

        rain_i = 0.0 if rain is None else clamp(
            float(rain) / max(_scalar(cfg, "rain_ref_mm_per_hr", 25.0), 1e-6), 0.0, 1.0)
        onset_c = _scalar(cfg, "heat_onset_c", 32.0)
        ref_c = max(_scalar(cfg, "heat_ref_c", 45.0), onset_c + 1e-6)
        heat_i = 0.0 if apparent is None else clamp((float(apparent) - onset_c) / (ref_c - onset_c), 0.0, 1.0)
        onset_w = _scalar(cfg, "wind_onset_kph", 39.0)
        ref_w = max(_scalar(cfg, "wind_ref_kph", 88.0), onset_w + 1e-6)
        wind_i = 0.0 if wind is None else clamp((float(wind) - onset_w) / (ref_w - onset_w), 0.0, 1.0)
        flood = float(flood_severity or 0.0)
        flood_i = 0.0 if flood < _scalar(cfg, "flood_onset", 0.05) else clamp(flood, 0.0, 1.0)
        return {"rain": round(rain_i, 4), "heat": round(heat_i, 4),
                "wind": round(wind_i, 4), "flood": round(flood_i, 4)}

    # --- the vector ----------------------------------------------------------
    def impact(self, conditions: dict[str, Any] | None, *, flood_severity: float = 0.0,
               flooded_entity_ids: list[str] | None = None, duration_sec: int | None = None,
               horizon_sec: int = 0, availability: str = "live", stale: bool = False) -> dict[str, Any]:
        """The impact vector for one weather reading.

        `conditions is None` (weather unavailable) yields the identity vector:
        every multiplier is 1.0, nothing is applied, and `applied` is False.
        That is the honest response to having no data — not a guessed calm day.
        """
        cfg = self.cfg
        if not conditions:
            return self._identity("No weather reading is available, so no weather effect is applied.",
                                  availability)

        drv = self.drivers(conditions, flood_severity)
        rain, heat, wind, flood = drv["rain"], drv["heat"], drv["wind"], drv["flood"]

        # widen(x) -> (low, high) for a value built from coefficient k over drivers
        widening = _scalar(cfg, "horizon_widening_per_hour", 0.06) * (max(0, int(horizon_sec)) / 3600.0)

        def effect(pairs: list[tuple[str, float]], *, direction: int, base: float = 1.0,
                   key: str | None = None) -> dict[str, Any]:
            """Compose one output scalar from (coefficient, driver) pairs.

            `direction` is +1 for a multiplier that grows with severity (travel
            time) and -1 for one that shrinks (service rate, capacity). The low
            and high variants use `k_min` / `k_max`, then the forecast horizon
            widens the band symmetrically.
            """
            central = high = low = base
            reasons: list[dict[str, Any]] = []
            for name, driver in pairs:
                if driver <= 0.0:
                    continue
                c = _coeff(cfg, name)
                central += direction * c["k"] * driver
                low += direction * c["k_min"] * driver
                high += direction * c["k_max"] * driver
                reasons.append({"rule": name, "driver": round(driver, 4), "coefficient": c["k"],
                                "basis": c["basis"], "kind": self.model_kind})
            span = abs(central - base) * widening
            lo, hi = sorted((low - span, high + span))
            out = {
                "value": round(central, 4), "lower": round(lo, 4), "upper": round(hi, 4),
                # `mild` and `severe` are the k_min and k_max variants of THIS
                # scalar. They are not the same as lower/upper: for a shrinking
                # multiplier (service rate, capacity, attendance) k_max produces
                # the *smaller* number, so `severe` is the band's lower edge.
                # A coherent scenario variant must mix by coefficient extreme,
                # not by band edge — mixing a mild travel coefficient with a
                # severe service one produced an outcome outside its own band.
                "mild": round(low, 4), "severe": round(high, 4),
                "rules": reasons,
                "uncertainty_basis": ("coefficient range + forecast horizon" if reasons and widening > 0
                                      else "coefficient range" if reasons else "no effect"),
            }
            if key:
                lo_lim, hi_lim = _band(cfg, key)
                for field in ("value", "lower", "upper", "mild", "severe"):
                    out[field] = round(clamp(out[field], lo_lim, hi_lim), 4)
            return out

        travel = effect([("rain_travel", rain), ("flood_travel", flood)], direction=+1, key="travel_time_mult")
        dwell = effect([("rain_dwell", rain), ("heat_dwell", heat)], direction=+1, key="dwell_mult")
        service = effect([("rain_service", rain), ("wind_service", wind)], direction=-1, key="service_rate_mult")
        attendance = effect([("rain_attendance", rain), ("heat_attendance", heat)], direction=-1,
                            key="attendance_mult")
        emergency = effect([("heat_emergency", heat)], direction=+1, key="emergency_gain_mult")
        rain_cap = effect([("rain_capacity", rain), ("wind_capacity", wind)], direction=-1, key="capacity_mult")
        flood_cap = effect([("flood_capacity", flood)], direction=-1, key="capacity_mult")
        arrival = effect([("rain_arrival_min", rain), ("heat_arrival_min", heat)], direction=+1, base=0.0,
                         key="arrival_shift_min")
        spread = effect([("rain_bunching", rain), ("heat_bunching", heat)], direction=-1,
                        key="arrival_spread_mult")

        # Capacity is per entity TYPE. Rain/wind hit roads and car parks;
        # flooding additionally hits surface transport nodes.
        type_capacity: dict[str, float] = {}
        for entity_type in RAIN_CAPACITY_TYPES:
            type_capacity[entity_type] = rain_cap["value"]
        if flood > 0.0:
            for entity_type in FLOOD_CAPACITY_TYPES:
                type_capacity[entity_type] = round(type_capacity.get(entity_type, 1.0) * flood_cap["value"], 4)

        # Closures are never inferred from an id or a hash. Only entities the
        # operator named in the scenario close, and only at extreme flooding.
        named = [str(e) for e in (flooded_entity_ids or [])]
        closure_at = _scalar(cfg, "flood_closure_severity", 0.80)
        closed = named if (named and flood >= closure_at) else []

        severity = conditions.get("severity") or "calm"
        applied = any([
            travel["value"] != 1.0, dwell["value"] != 1.0, service["value"] != 1.0,
            attendance["value"] != 1.0, emergency["value"] != 1.0, arrival["value"] != 0.0,
            spread["value"] != 1.0,
            any(abs(v - 1.0) > 1e-9 for v in type_capacity.values()), bool(closed),
        ])
        return {
            "model": self.model_version, "model_kind": self.model_kind, "applied": applied,
            "availability": availability, "severity": severity,
            "drivers": drv,
            "horizon_sec": int(max(0, horizon_sec)),
            "duration_sec": None if duration_sec is None else int(max(0, duration_sec)),
            "confidence": self._confidence(availability, horizon_sec, stale),
            # --- the vector the city model applies -------------------------
            "travel_time_mult": travel["value"],
            "dwell_mult": dwell["value"],
            "service_rate_mult": service["value"],
            "attendance_mult": attendance["value"],
            "arrival_shift_min": arrival["value"],
            "arrival_spread_mult": spread["value"],
            "emergency_gain_mult": emergency["value"],
            "type_capacity_mult": type_capacity,
            "closed_entity_ids": closed,
            # --- the same numbers with their bands and their reasons --------
            "bands": {
                "travel_time_mult": travel, "dwell_mult": dwell, "service_rate_mult": service,
                "attendance_mult": attendance, "arrival_shift_min": arrival,
                "arrival_spread_mult": spread,
                "emergency_gain_mult": emergency, "capacity_mult": rain_cap,
                **({"flood_capacity_mult": flood_cap} if flood > 0.0 else {}),
            },
            "causal_chain": [dict(link) for link in CAUSAL_CHAIN],
            "note": None if applied else "The reading is calm enough that no effect is modelled.",
        }

    def _identity(self, note: str, availability: str) -> dict[str, Any]:
        return {
            "model": self.model_version, "model_kind": self.model_kind, "applied": False,
            "availability": availability, "severity": "calm",
            "drivers": {"rain": 0.0, "heat": 0.0, "wind": 0.0, "flood": 0.0},
            "horizon_sec": 0, "duration_sec": None, "confidence": "low",
            "travel_time_mult": 1.0, "dwell_mult": 1.0, "service_rate_mult": 1.0,
            "attendance_mult": 1.0, "arrival_shift_min": 0.0, "arrival_spread_mult": 1.0,
            "emergency_gain_mult": 1.0,
            "type_capacity_mult": {}, "closed_entity_ids": [], "bands": {},
            "causal_chain": [dict(link) for link in CAUSAL_CHAIN], "note": note,
        }

    def _confidence(self, availability: str, horizon_sec: int, stale: bool) -> str:
        if stale or availability == "unavailable":
            return str(self.cfg.get("stale_confidence", DEFAULTS["stale_confidence"]))
        if horizon_sec >= _scalar(self.cfg, "low_confidence_after_sec", 10800):
            return "low"
        if availability in ("cached", "synthetic") or horizon_sec > 0:
            return "medium"
        return "high"

    # --- forecast track ------------------------------------------------------
    def forecast_impacts(self, forecast: list[dict[str, Any]], *, availability: str = "live",
                         limit: int = 12) -> list[dict[str, Any]]:
        """One impact vector per forecast hour — the twin's expected trajectory
        of weather pressure, each point carrying its own widened band."""
        out = []
        for point in (forecast or [])[:max(0, int(limit))]:
            vector = self.impact(point, horizon_sec=int(point.get("horizon_sec") or 0),
                                 availability=availability)
            out.append({
                "valid_at": point.get("valid_at"), "horizon_sec": int(point.get("horizon_sec") or 0),
                "severity": point.get("severity"), "conditions": point,
                "travel_time_mult": vector["travel_time_mult"],
                "service_rate_mult": vector["service_rate_mult"],
                "attendance_mult": vector["attendance_mult"],
                "confidence": vector["confidence"],
                "bands": {k: vector["bands"][k] for k in ("travel_time_mult", "service_rate_mult")
                          if k in vector["bands"]},
            })
        return out


def conditions_from_scenario(params: dict[str, Any], base: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build a weather reading from what-if parameters, on top of `base` (the
    live reading) so an unspecified field keeps its real current value rather
    than jumping to an arbitrary default.

    Accepted params: `rain_mm_per_hr`, `temp_c`, `apparent_temp_c`, `wind_kph`,
    `humidity`, `flood_severity`, `storm_duration_min`, `condition_code`.
    """
    from ..providers.weather import build_conditions

    src = dict(base or {})
    temp = params.get("temp_c", src.get("temp_c"))
    apparent = params.get("apparent_temp_c", params.get("temp_c", src.get("apparent_temp_c")))
    rain = params.get("rain_mm_per_hr", params.get("precipitation_mm_per_hr", src.get("precipitation_mm_per_hr")))
    return build_conditions(
        valid_at=src.get("valid_at") or "1970-01-01T00:00:00Z",
        temp_c=None if temp is None else float(temp),
        apparent_temp_c=None if apparent is None else float(apparent),
        precipitation_mm_per_hr=None if rain is None else float(rain),
        wind_kph=float(params["wind_kph"]) if params.get("wind_kph") is not None else src.get("wind_kph"),
        humidity=float(params["humidity"]) if params.get("humidity") is not None else src.get("humidity"),
        condition_code=int(params["condition_code"]) if params.get("condition_code") is not None
        else src.get("condition_code"),
        horizon_sec=0,
    )
