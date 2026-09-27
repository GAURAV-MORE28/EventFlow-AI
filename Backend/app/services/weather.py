"""Weather in the loop: fetch → impact → (optionally) the live simulation.

This service owns the weather side of the digital twin. It does **not** own a
second twin: the twin is the existing `Engine` (live world + nominal process
model + EnKF assimilation), and weather enters it the same way every other
condition does — as a modifier on the city model, which the existing cycle then
forecasts, risk-scores and cascades from.

    ┌ WeatherProvider (live Open-Meteo)      providers/weather.py
    │       ↓ WeatherConditions
    ├ WeatherImpactModel (rule-based)        services/weather_impact.py
    │       ↓ ImpactVector
    └ generator.inject("weather", …)         ml_reference/generator.py
            ↓  the ordinary cycle does the rest
      travel time · queues · gate spill · venue load · cascades · risk

## Advisory by default

`weather.drive_live` is **false** out of the box. Weather is always fetched,
always shown, and always available to what-if — but it only becomes a modifier
on the live city when an operator switches it on (`POST /weather/apply`) or the
config says to. That keeps the seeded demo run reproducible from the seed alone
(CLAUDE.md: `sim_time` is fully determined by the seed; never introduce
unseeded inputs into the cycle) while still letting the operator show a real
live-weather-driven twin on demand. `driving_live` is reported on every payload,
so the UI never implies the simulation is following the weather when it is not.

When it *is* driving the live city, the same vector is applied to the nominal
world too: a forecast is announced information, so the twin's process model
should know about it, and assimilation then only has to correct the gap between
the forecast and what the sensors actually see.

## Location

The location is the **active world's** venue coordinates, so activating a
blueprint for another city moves the weather query with it. No coordinate and no
city name is hardcoded anywhere in this module.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any

from ..providers.weather import get_weather_provider
from ..simtime import server_now
from .weather_impact import WeatherImpactModel, conditions_from_scenario

log = logging.getLogger("eventflow.weather")

#: One stable id, so a refresh replaces the previous vector instead of stacking.
WEATHER_MODIFIER_ID = "mod_weather_live"
DEFAULT_REFRESH_SEC = 900


class WeatherService:
    """Live weather state, its impact vector, and its optional effect on the city."""

    def __init__(self, engine: Any) -> None:
        self.engine = engine
        cfg = dict(engine.config.raw.get("weather") or {})
        self.cfg = cfg
        self.model = WeatherImpactModel(cfg)
        self.refresh_sec = max(60, int(cfg.get("refresh_sec", DEFAULT_REFRESH_SEC)))
        self.forecast_limit = int(cfg.get("forecast_hours", 12))
        # Advisory unless configuration or an operator says otherwise.
        self.driving_live = bool(cfg.get("drive_live", False))
        self.state: dict[str, Any] | None = None
        self.impact: dict[str, Any] | None = None
        self.forecast_impacts: list[dict[str, Any]] = []
        self.last_refresh_monotonic: float | None = None
        self.last_error: str | None = None
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self._applied_signature: tuple | None = None

    # --- location ------------------------------------------------------------
    def location(self) -> dict[str, Any]:
        """Where to ask about — the active world's venue, falling back to the
        centre of its footprint and finally to any node with coordinates."""
        engine = self.engine
        world = engine.world
        nodes = engine.store.nodes
        venue_id = world.get("venue_entity_id")
        node = nodes.get(venue_id or "")
        if node and node.get("lat") is not None:
            return {"lat": float(node["lat"]), "lon": float(node["lon"]),
                    "label": node.get("display_name") or venue_id, "entity_id": venue_id}
        footprint = world.get("footprint") or {}
        if footprint.get("centre_lat") is not None:
            return {"lat": float(footprint["centre_lat"]), "lon": float(footprint["centre_lon"]),
                    "label": world.get("world_id"), "entity_id": None}
        for eid, n in nodes.items():
            if n.get("lat") is not None:
                return {"lat": float(n["lat"]), "lon": float(n["lon"]),
                        "label": n.get("display_name") or eid, "entity_id": eid}
        return {"lat": 0.0, "lon": 0.0, "label": world.get("world_id"), "entity_id": None}

    def topics(self) -> list[str]:
        """Search topics for the public-signal provider: the venue and, when a
        blueprint supplied one, the city it sits in. Derived from the active
        world — never a hardcoded city."""
        engine = self.engine
        world = engine.world
        out: list[str] = []
        venue_id = world.get("venue_entity_id")
        node = engine.store.nodes.get(venue_id or "")
        if node and node.get("display_name"):
            out.append(str(node["display_name"]))
        for key in ("city", "locality", "venue_city"):
            value = (world.get("footprint") or {}).get(key) or world.get(key)
            if value:
                out.append(str(value))
        attribution = world.get("attribution") or {}
        if isinstance(attribution, dict) and attribution.get("city"):
            out.append(str(attribution["city"]))
        seen: list[str] = []
        for topic in out:
            if topic not in seen:
                seen.append(topic)
        return seen

    # --- refresh -------------------------------------------------------------
    def refresh_sync(self) -> bool:
        """Fetch and recompute. Returns True when the impact vector changed
        materially (worth a reconcile / broadcast). Never raises."""
        location = self.location()
        provider = get_weather_provider()
        try:
            state = provider.observe(location["lat"], location["lon"], location.get("label"))
        except Exception as exc:                            # a provider must not break the cycle
            log.exception("weather provider raised")
            self.last_error = f"{type(exc).__name__}"
            from ..providers.weather import unavailable

            state = unavailable(location, f"provider raised {type(exc).__name__}",
                                getattr(provider, "name", "unknown"))
        else:
            self.last_error = state.get("detail") if state.get("availability") == "unavailable" else None
        state["location"] = {**state.get("location", {}), **{k: v for k, v in location.items() if k == "entity_id"}}
        impact = self.model.impact(
            state.get("current"), horizon_sec=0,
            availability=state.get("availability", "unavailable"), stale=bool(state.get("stale")),
        )
        self.forecast_impacts = self.model.forecast_impacts(
            state.get("forecast") or [], availability=state.get("availability", "unavailable"),
            limit=self.forecast_limit)
        previous = self._signature(self.impact)
        self.state, self.impact = state, impact
        self.last_refresh_monotonic = time.monotonic()
        changed = self._signature(impact) != previous
        if self.driving_live:
            changed = self.sync_live_modifier() or changed
        return changed

    @staticmethod
    def _signature(impact: dict | None) -> tuple:
        """What counts as a material change — the numbers the city actually
        applies, rounded, so sensor jitter does not trigger a reconcile."""
        if not impact:
            return ()
        return (
            round(float(impact.get("travel_time_mult", 1.0)), 3),
            round(float(impact.get("dwell_mult", 1.0)), 3),
            round(float(impact.get("service_rate_mult", 1.0)), 3),
            round(float(impact.get("attendance_mult", 1.0)), 3),
            round(float(impact.get("arrival_shift_min", 0.0)), 1),
            round(float(impact.get("arrival_spread_mult", 1.0)), 3),
            round(float(impact.get("emergency_gain_mult", 1.0)), 3),
            tuple(sorted((k, round(float(v), 3)) for k, v in (impact.get("type_capacity_mult") or {}).items())),
            tuple(sorted(impact.get("closed_entity_ids") or [])),
        )

    # --- effect on the live city --------------------------------------------
    def sync_live_modifier(self) -> bool:
        """Make the live world's `weather` modifier match `driving_live` and the
        current impact vector. Returns True when the city changed."""
        engine = self.engine
        target = self.impact if (self.driving_live and self.impact and self.impact.get("applied")) else None
        signature = self._signature(target) if target else None
        if signature == self._applied_signature:
            return False
        with engine.world_lock:
            worlds = [engine.generator] + ([engine.nominal] if engine.nominal is not None else [])
            for world in worlds:
                if not hasattr(world, "remove_modifier"):
                    continue
                world.remove_modifier(WEATHER_MODIFIER_ID)
                if target is not None:
                    world.inject("weather", {"impact": target}, source="weather",
                                 modifier_id=WEATHER_MODIFIER_ID)
            # A counterfactual is "the city without this intervention", not
            # "the city without the weather": it keeps the same conditions.
            for cf in engine.counterfactuals.values():
                if hasattr(cf, "remove_modifier"):
                    cf.remove_modifier(WEATHER_MODIFIER_ID)
                    if target is not None:
                        cf.inject("weather", {"impact": target}, source="weather",
                                  modifier_id=WEATHER_MODIFIER_ID)
        self._applied_signature = signature
        engine.world_changed()
        log.info("weather modifier %s (severity=%s, travel x%.3f)",
                 "applied" if target else "cleared",
                 (target or {}).get("severity", "—"), (target or {}).get("travel_time_mult", 1.0))
        return True

    async def set_driving_live(self, enabled: bool) -> dict[str, Any]:
        """Operator switch. Turning it on applies the current vector to the live
        city and reconciles at once, so the effect is visible immediately
        instead of at the next cycle."""
        was = self.driving_live
        self.driving_live = bool(enabled)
        if self.state is None:
            await asyncio.to_thread(self.refresh_sync)
        changed = await asyncio.to_thread(self.sync_live_modifier)
        if changed or was != self.driving_live:
            await self.engine.reconcile("weather_applied" if self.driving_live else "weather_cleared")
        return self.payload()

    def on_world_changed(self) -> None:
        """A new world was activated: the old reading was for the old venue and
        the old world's modifier list is gone with it."""
        self.state = None
        self.impact = None
        self.forecast_impacts = []
        self._applied_signature = None
        self.last_refresh_monotonic = None

    # --- background refresh --------------------------------------------------
    async def start(self) -> None:
        self._stopping.clear()
        self._task = asyncio.create_task(self._loop(), name="eventflow-weather")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._task = None

    async def _loop(self) -> None:
        while not self._stopping.is_set():
            try:
                changed = await asyncio.to_thread(self.refresh_sync)
                await self.broadcast()
                if changed and self.driving_live:
                    # Real-world data moved: re-state the current instant now,
                    # exactly as an operator change does.
                    await self.engine.reconcile("weather_updated")
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("weather refresh failed; continuing")
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self.refresh_sec)
            except asyncio.TimeoutError:
                continue

    async def broadcast(self) -> None:
        from ..ws.manager import MANAGER

        await MANAGER.broadcast("weather_update", self.payload(), self.engine.store.sim_time)

    # --- payloads ------------------------------------------------------------
    def payload(self) -> dict[str, Any]:
        """`GET /weather` — reading, forecast, impact vector, and an explicit
        statement of whether any of it is reaching the live simulation."""
        state = self.state
        impact = self.impact or self.model.impact(None)
        age = None
        if self.last_refresh_monotonic is not None:
            age = int(round(time.monotonic() - self.last_refresh_monotonic))
        return {
            "server_time": server_now(),
            "sim_time": self.engine.store.sim_time,
            "world_id": self.engine.world["world_id"],
            "provider": (state or {}).get("provider", getattr(get_weather_provider(), "name", "unknown")),
            "availability": (state or {}).get("availability", "unavailable"),
            "source": (state or {}).get("source", "unavailable"),
            "observed_at": (state or {}).get("observed_at"),
            "fetched_at": (state or {}).get("fetched_at"),
            "observation_age_sec": (state or {}).get("age_sec"),
            "refreshed_age_sec": age,
            "refresh_interval_sec": self.refresh_sec,
            "stale": bool((state or {}).get("stale", True)),
            "location": (state or {}).get("location") or self.location(),
            "current": (state or {}).get("current"),
            "forecast": (state or {}).get("forecast") or [],
            "impact": impact,
            "forecast_impacts": list(self.forecast_impacts),
            # The honesty flags the whole feature turns on.
            "driving_live": bool(self.driving_live),
            "applied_to_live": bool(self.driving_live and impact.get("applied")),
            "detail": (state or {}).get("detail") or self.last_error,
        }

    # --- what-if -------------------------------------------------------------
    def scenario_conditions(self, params: dict[str, Any]) -> dict[str, Any]:
        """Weather conditions for a what-if, layered on the live reading so an
        unspecified field keeps its real current value."""
        return conditions_from_scenario(params, (self.state or {}).get("current"))

    def scenario_impact(self, params: dict[str, Any]) -> dict[str, Any]:
        """The impact vector for a what-if's weather parameters."""
        conditions = self.scenario_conditions(params)
        duration_min = params.get("storm_duration_min")
        return self.model.impact(
            conditions,
            flood_severity=float(params.get("flood_severity") or 0.0),
            flooded_entity_ids=[str(e) for e in (params.get("flooded_entity_ids") or [])],
            duration_sec=None if duration_min is None else int(float(duration_min) * 60),
            horizon_sec=0,
            availability="scenario",
        )

    def expand_scenarios(self, scenarios: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Translate every `weather_scenario` into the generator-level `weather`
        modifier that carries its computed impact vector.

        The simulator never sees weather parameters, only an impact vector — the
        translation lives here, so there is exactly one weather→numbers model
        and the city model stays a city model. A `combined` scenario is expanded
        recursively so weather can be mixed with any other change.
        """
        out: list[dict[str, Any]] = []
        for scenario in scenarios or []:
            kind = scenario.get("scenario_type")
            params = dict(scenario.get("params") or {})
            if kind == "weather_scenario":
                out.append({"scenario_type": "weather", "params": {"impact": self.scenario_impact(params)}})
            elif kind == "combined":
                out.append({"scenario_type": "combined",
                            "params": {**params, "scenarios": self.expand_scenarios(params.get("scenarios") or [])}})
            else:
                out.append({"scenario_type": kind, "params": params})
        return out

    #: Scalars that make up a coherent scenario variant.
    VARIANT_FIELDS = ("travel_time_mult", "dwell_mult", "service_rate_mult", "attendance_mult",
                      "emergency_gain_mult", "arrival_shift_min", "arrival_spread_mult")

    def scenario_band_impacts(self, params: dict[str, Any]) -> dict[str, dict[str, Any]] | None:
        """The *mild* and *severe* impact vectors for a weather scenario.

        Running the scenario three times (mild / central / severe) turns the
        rules' coefficient uncertainty into an uncertainty band on the
        **outcome** — "mean trip time 46 min (43–52)" — instead of a confidence
        number with nothing behind it.

        A variant must be coherent: every scalar takes the value produced by the
        *same* coefficient extreme. That is what `mild` / `severe` on each band
        are for. Selecting by band edge instead mixes a weak travel effect with
        a strong service effect and yields an outcome outside its own band.
        """
        central = self.scenario_impact(params)
        bands = central.get("bands") or {}
        if not central.get("applied") or not bands:
            return None

        def variant(edge: str) -> dict[str, Any]:
            out = dict(central)
            for key in self.VARIANT_FIELDS:
                band = bands.get(key)
                if isinstance(band, dict) and band.get(edge) is not None:
                    out[key] = float(band[edge])
            cap = bands.get("capacity_mult") or {}
            flood = bands.get("flood_capacity_mult") or {}
            scale = float(cap.get(edge, 1.0)) if cap else 1.0
            flood_scale = float(flood.get(edge, 1.0)) if flood else 1.0
            out["type_capacity_mult"] = {
                etype: round(min(1.0, scale * (flood_scale if etype in ("road", "parking", "transport_node")
                                               and flood else 1.0)), 4)
                for etype in (central.get("type_capacity_mult") or {})
            }
            out["band_edge"] = edge
            return out

        return {"mild": variant("mild"), "severe": variant("severe")}

    def health(self) -> dict[str, Any]:
        """`GET /health` module entry. `ready` means "we have a reading"."""
        state = self.state
        availability = (state or {}).get("availability", "unavailable")
        return {
            "ready": bool(state and state.get("current")),
            "active_source": (state or {}).get("source", "unavailable"),
            "detail": (f"{availability}; driving_live={str(self.driving_live).lower()}"
                       + (f"; {state['detail']}" if state and state.get("detail") else "")),
        }
