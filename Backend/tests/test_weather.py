"""The weather-driven digital twin, end to end.

The acceptance chain this file exists to pin down:

    REAL WEATHER INPUT → DIGITAL TWIN STATE CHANGE → CASCADING EVENTFLOW CHANGE
    → INTERACTIVE WEATHER WHAT-IF → NO MUTATION OF LIVE STATE

Nothing here touches the network: the weather provider's HTTP transport and the
social provider's are both injected, so every failure mode (timeout, HTTP error,
malformed JSON, missing forecast, rate limit) is exercised deterministically.
"""
from __future__ import annotations

import asyncio
import json
import math

import pytest
from fastapi.testclient import TestClient

from app import schemas as S
from app.geospatial.http import HttpClient
from app.main import app
from app.providers import social as SOC
from app.providers import weather as W
from app.services.engine import get_engine
from app.services.projection import critical_map, run_forward, summarise_side
from app.services.simulation import SIMULATIONS
from app.services.weather_impact import WeatherImpactModel, conditions_from_scenario

API = "/api/v1"


# --- fixtures ----------------------------------------------------------------------------------
def _run(engine, cycles: int) -> None:
    loop = asyncio.new_event_loop()
    try:
        for _ in range(cycles):
            loop.run_until_complete(engine.run_cycle())
    finally:
        loop.close()


def _await(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def live_body(*, temp=31.0, rain=0.0, wind=10.0, code=0, hours=6, humidity=62.0):
    """An Open-Meteo shaped payload. `timezone=UTC` really does return times
    without a zone suffix, which is the parsing case that matters."""
    times = [f"2026-09-04T{h:02d}:00" for h in range(12, 12 + hours + 1)]
    return {
        "latitude": 23.09, "longitude": 72.6,
        "current": {"time": "2026-09-04T12:30", "temperature_2m": temp, "relative_humidity_2m": humidity,
                    "apparent_temperature": temp + 3.0, "precipitation": rain, "rain": rain,
                    "weather_code": code, "wind_speed_10m": wind, "wind_gusts_10m": wind * 1.4},
        "hourly": {
            "time": times,
            "temperature_2m": [temp + i * 0.5 for i in range(len(times))],
            "apparent_temperature": [temp + 3.0 + i * 0.5 for i in range(len(times))],
            "precipitation": [rain for _ in times],
            "precipitation_probability": [40 for _ in times],
            "weather_code": [code for _ in times],
            "wind_speed_10m": [wind for _ in times],
        },
    }


def provider_with(responses, **kw):
    """An OpenMeteoWeather whose transport returns `responses` in order.
    An entry may be a dict (200 + that JSON), an int (that HTTP status) or an
    exception instance (raised, i.e. a network failure)."""
    state = {"i": 0}

    def transport(method, url, headers, body, timeout):
        item = responses[min(state["i"], len(responses) - 1)]
        state["i"] += 1
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, int):
            return item, b'{"error": true}'
        if isinstance(item, bytes):
            return 200, item
        return 200, json.dumps(item).encode()

    client = HttpClient(user_agent="test", timeout_sec=0.2, retries=0, cache_ttl_sec=0.0,
                        transport=transport)
    return W.OpenMeteoWeather(client=client, **kw), state


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        engine = get_engine()
        engine.paused = True
        # A deterministic offline reading, so no test depends on today's weather.
        W.set_weather_provider(W.SyntheticWeather(seed=42))
        SOC.set_social_provider(SOC.FixtureSignals())
        engine.weather.driving_live = False
        engine.weather.refresh_sync()
        _run(engine, 120)                 # 15:00 — the arrival wave is building
        yield c
        _await(engine.weather.set_driving_live(False))
    W.set_weather_provider(None)
    SOC.set_social_provider(None)


@pytest.fixture
def model():
    """The impact model needs configuration only — no engine, no state. That is
    the point of keeping it pure."""
    from app.config import get_config

    return WeatherImpactModel(get_config().raw.get("weather"))


# --- A. the provider: live, cached, unavailable — never a lie ------------------------------------
def test_live_payload_is_normalised_to_contract_units():
    provider, _ = provider_with([live_body(temp=30.0, rain=12.0, wind=45.0, code=63)])
    state = provider.observe(23.09, 72.6, "Test Stadium")

    assert state["availability"] == "live" and state["source"] == "open_meteo"
    current = state["current"]
    assert current["temp_c"] == 30.0 and current["precipitation_mm_per_hr"] == 12.0
    assert current["wind_kph"] == 45.0 and current["condition"] == "moderate rain"
    # A percentage on the wire is a fraction 0.0-1.0 (00 §0), not 0-100.
    assert 0.0 <= current["humidity"] <= 1.0 and current["humidity"] == pytest.approx(0.62)
    # ISO 8601 UTC with a Z, from a provider timestamp that carried no zone.
    assert current["valid_at"].endswith("Z") and state["observed_at"] == "2026-09-04T12:30:00Z"
    assert current["severity"] == "severe"          # 12 mm/hr is the severe rain band
    assert state["forecast"] and all(p["horizon_sec"] > 0 for p in state["forecast"])
    assert all(p["valid_at"].endswith("Z") for p in state["forecast"])
    assert state["location"] == {"lat": 23.09, "lon": 72.6, "label": "Test Stadium"}


def test_severity_bands_follow_the_measured_values():
    def sev(**kw):
        return W.build_conditions(valid_at="2026-09-04T12:00:00Z", temp_c=kw.get("t", 28.0),
                                  apparent_temp_c=kw.get("a"), precipitation_mm_per_hr=kw.get("r", 0.0),
                                  wind_kph=kw.get("w", 5.0))["severity"]

    assert sev() == "calm"
    assert sev(r=4.0) == "moderate"
    assert sev(r=10.0) == "severe"
    assert sev(r=35.0) == "extreme"
    assert sev(w=70.0) == "severe"               # wind alone can set the band
    assert sev(a=46.0) == "extreme"              # so can apparent temperature
    # The worst driver wins, never an average.
    assert sev(r=35.0, w=5.0, a=20.0) == "extreme"


@pytest.mark.parametrize("failure", [
    OSError("connection reset"), TimeoutError("timed out"), 500, 429,
    b"<html>not json</html>",
    {"latitude": 1.0},                            # 200 OK, but no `current` block
])
def test_every_failure_mode_is_unavailable_not_invented(failure):
    provider, _ = provider_with([failure])
    state = provider.observe(23.09, 72.6, "Test Stadium")

    assert state["availability"] == "unavailable"
    assert state["current"] is None and state["forecast"] == []
    assert state["detail"]                        # says why
    # The one thing that must never happen.
    assert state["source"] != "open_meteo" and "live" not in state["source"]


def test_a_failing_provider_serves_the_last_good_reading_as_cached():
    provider, _ = provider_with([live_body(rain=8.0), OSError("down"), OSError("down")])
    first = provider.observe(23.09, 72.6)
    assert first["availability"] == "live"

    second = provider.observe(23.09, 72.6)
    assert second["availability"] == "cached" and second["source"] == "cached:open_meteo"
    # Same reading, honestly aged and honestly labelled.
    assert second["current"] == first["current"]
    assert second["age_sec"] >= first["age_sec"]
    assert "Live fetch failed" in second["detail"]
    assert provider.stats["cached_answers"] == 1


def test_a_cached_reading_is_flagged_stale_once_it_is_old():
    provider, _ = provider_with([live_body(), OSError("down")], stale_after_sec=0.0)
    provider.observe(23.09, 72.6)
    assert provider.observe(23.09, 72.6)["stale"] is True


def test_a_missing_hourly_block_is_an_empty_forecast_not_an_error():
    body = live_body()
    body.pop("hourly")
    provider, _ = provider_with([body])
    state = provider.observe(23.09, 72.6)

    assert state["availability"] == "live" and state["current"] is not None
    assert state["forecast"] == []
    assert "no hourly forecast" in state["detail"]


def test_a_malformed_hourly_column_is_skipped_not_guessed():
    body = live_body(hours=3)
    body["hourly"]["precipitation"] = [1.0]              # wrong length
    body["hourly"]["temperature_2m"] = [20.0, None, "x", 23.0]
    provider, _ = provider_with([body])
    points = provider.observe(23.09, 72.6)["forecast"]

    assert points
    # A column the provider mangled reads as unknown (null), never as 0.0.
    assert all(p["precipitation_mm_per_hr"] is None for p in points)
    assert any(p["temp_c"] is None for p in points)


def test_synthetic_weather_is_deterministic_and_never_claims_to_be_observed():
    a = W.SyntheticWeather(seed=42).observe(23.09, 72.6, "X")
    b = W.SyntheticWeather(seed=42).observe(23.09, 72.6, "X")
    assert a["current"] == b["current"] and a["forecast"] == b["forecast"]
    assert a["availability"] == "synthetic" and a["source"] == "synthetic"
    assert "not an observation" in a["detail"]
    # Two locations do not share one curve.
    assert W.SyntheticWeather(seed=42).observe(51.5, -0.12)["current"] != a["current"]


def test_the_synthetic_providers_baseline_is_configurable():
    """An offline run can stand in for a particular kind of day — used by the
    tests below and by the severe-weather UI paths — while still reporting
    `synthetic`, never `live`."""
    heavy = W.build_weather_provider(
        {"provider": "synthetic", "base": {"rain_mm_per_hr": 26.0, "temp_c": 31.0, "wind_kph": 45.0}},
        env={})
    state = heavy.observe(23.09, 72.6, "Test Stadium")
    assert state["availability"] == "synthetic"
    assert state["current"]["precipitation_mm_per_hr"] > 20.0
    assert state["current"]["severity"] in ("severe", "extreme")
    # Still deterministic with a base set.
    assert heavy.observe(23.09, 72.6, "Test Stadium")["forecast"] == state["forecast"]
    # And the default baseline is a calm day, so nothing changes for other tests.
    calm = W.build_weather_provider({"provider": "synthetic"}, env={}).observe(23.09, 72.6)
    assert calm["current"]["precipitation_mm_per_hr"] < 5.0


def test_provider_selection_from_config_and_environment():
    assert W.build_weather_provider({"provider": "none"}, env={}).name == "none"
    assert W.build_weather_provider({"provider": "synthetic"}, env={}).name == "synthetic"
    assert W.build_weather_provider({}, env={}).name == "open_meteo"
    assert W.build_weather_provider({"provider": "open_meteo"},
                                    env={"EVENTFLOW_WEATHER_PROVIDER": "none"}).name == "none"
    # An unknown name falls back to the real provider rather than failing to start.
    assert W.build_weather_provider({"provider": "nonsense"}, env={}).name == "open_meteo"
    assert W.NoWeather().observe(1.0, 2.0)["availability"] == "unavailable"


# --- B. the impact model: rule-based, reproducible, bounded ---------------------------------------
def conditions(**kw):
    return conditions_from_scenario(kw)


def test_identical_input_gives_an_identical_impact_vector(model):
    args = dict(rain_mm_per_hr=14.0, temp_c=38.0, wind_kph=50.0)
    first = model.impact(conditions(**args), flood_severity=0.4)
    second = model.impact(conditions(**args), flood_severity=0.4)
    assert first == second
    # A second model instance built from the same config agrees.
    from app.config import get_config

    twin = WeatherImpactModel(get_config().raw.get("weather"))
    assert twin.impact(conditions(**args), flood_severity=0.4) == first


def test_impact_is_monotone_in_rainfall(model):
    vectors = [model.impact(conditions(rain_mm_per_hr=r, temp_c=29.0)) for r in (0.0, 2.0, 8.0, 20.0, 40.0)]
    travel = [v["travel_time_mult"] for v in vectors]
    service = [v["service_rate_mult"] for v in vectors]
    road_cap = [v["type_capacity_mult"].get("road", 1.0) for v in vectors]
    spread = [v["arrival_spread_mult"] for v in vectors]

    assert travel == sorted(travel), travel                    # slower travel
    assert service == sorted(service, reverse=True), service    # slower service
    assert road_cap == sorted(road_cap, reverse=True)           # less road capacity
    assert spread == sorted(spread, reverse=True)               # arrivals bunch up
    # Saturating, so no rainfall figure can produce an absurd multiplier.
    assert travel[-1] <= 2.5 and service[-1] >= 0.4


def test_extreme_heat_drives_dwell_and_emergency_load_not_rainfall_effects(model):
    hot = model.impact(conditions(rain_mm_per_hr=0.0, temp_c=44.0))
    mild = model.impact(conditions(rain_mm_per_hr=0.0, temp_c=29.0))

    assert hot["dwell_mult"] > mild["dwell_mult"]
    assert hot["emergency_gain_mult"] > 1.0 and mild["emergency_gain_mult"] == 1.0
    assert hot["drivers"]["heat"] > 0 and hot["drivers"]["rain"] == 0.0
    # Heat is not rain: it must not reduce road capacity.
    assert hot["type_capacity_mult"].get("road", 1.0) == 1.0


def test_no_reading_applies_nothing_rather_than_guessing_a_calm_day(model):
    identity = model.impact(None, availability="unavailable")
    assert identity["applied"] is False
    for key in ("travel_time_mult", "dwell_mult", "service_rate_mult", "attendance_mult",
                "arrival_spread_mult", "emergency_gain_mult"):
        assert identity[key] == 1.0, key
    assert identity["arrival_shift_min"] == 0.0
    assert identity["type_capacity_mult"] == {} and identity["closed_entity_ids"] == []
    assert "no weather reading" in identity["note"].lower()


def test_an_unknown_measurement_is_not_treated_as_zero(model):
    """A provider that sent no rainfall figure means "unknown", so no rain effect
    is modelled — but that is not the same as a *reported* 0.0 mm/hr."""
    unknown = model.drivers(conditions(temp_c=29.0))          # no rain key at all
    reported = model.drivers(conditions(temp_c=29.0, rain_mm_per_hr=0.0))
    assert unknown["rain"] == reported["rain"] == 0.0
    # And a real value does move it, so the zero above is not a stuck driver.
    assert model.drivers(conditions(rain_mm_per_hr=10.0))["rain"] > 0.0


def test_uncertainty_bands_bracket_the_value_and_widen_with_the_forecast_horizon(model):
    now = model.impact(conditions(rain_mm_per_hr=15.0, temp_c=30.0), horizon_sec=0)
    later = model.impact(conditions(rain_mm_per_hr=15.0, temp_c=30.0), horizon_sec=6 * 3600)

    band = now["bands"]["travel_time_mult"]
    assert band["lower"] <= band["value"] <= band["upper"]
    assert band["lower"] < band["upper"], "a rule with a coefficient range must report a range"
    # Every band names the rules and the basis behind it — no bare confidence number.
    assert band["rules"] and all(r["basis"] and r["kind"] == "rule_based" for r in band["rules"])
    assert "coefficient range" in band["uncertainty_basis"]

    wide = later["bands"]["travel_time_mult"]
    assert (wide["upper"] - wide["lower"]) > (band["upper"] - band["lower"])
    assert now["confidence"] == "high" and later["confidence"] == "low"
    assert model.impact(conditions(rain_mm_per_hr=15.0), stale=True)["confidence"] == "low"


def test_flooding_closes_only_entities_the_operator_named(client, model):
    engine = get_engine()
    a_road = next(e for e, n in engine.store.nodes.items() if n["entity_type"] == "road")

    severe = model.impact(conditions(rain_mm_per_hr=30.0), flood_severity=0.95)
    assert severe["closed_entity_ids"] == [], "flooding must never guess which entities are under water"

    named = model.impact(conditions(rain_mm_per_hr=30.0), flood_severity=0.95,
                         flooded_entity_ids=[a_road])
    assert named["closed_entity_ids"] == [a_road]
    # Below the closure threshold a named entity loses capacity but stays open.
    mild = model.impact(conditions(rain_mm_per_hr=30.0), flood_severity=0.2,
                        flooded_entity_ids=[a_road])
    assert mild["closed_entity_ids"] == []
    assert mild["type_capacity_mult"]["road"] < 1.0


def test_capacity_loss_is_keyed_by_entity_type_never_by_entity_id(client, model):
    """Behaviour comes from `entity_type`, never from ids (CLAUDE.md): a
    generated OSM world with opaque hash ids must behave like the demo city."""
    vector = model.impact(conditions(rain_mm_per_hr=25.0), flood_severity=0.6)
    types = set(S.EntityType.__args__)
    assert vector["type_capacity_mult"], "rain must reduce some capacity"
    assert set(vector["type_capacity_mult"]) <= types
    ids = set(get_engine().store.nodes)
    assert not (set(vector["type_capacity_mult"]) & ids)


def test_forecast_impacts_track_the_forecast_hour_by_hour(model):
    provider, _ = provider_with([live_body(rain=9.0, hours=6)])
    state = provider.observe(23.09, 72.6)
    track = model.forecast_impacts(state["forecast"], availability="live")

    assert len(track) == len(state["forecast"]) >= 3
    assert [p["horizon_sec"] for p in track] == sorted(p["horizon_sec"] for p in track)
    assert all(p["travel_time_mult"] > 1.0 for p in track)      # 9 mm/hr slows travel
    # Confidence degrades with distance; it is never "high" for a forecast.
    assert all(p["confidence"] in ("medium", "low") for p in track)


# --- C. the city model: a weather modifier really moves the simulation ------------------------------
def warm_clone(engine):
    """A clone of the live city as the `client` fixture left it: 15:00, with the
    arrival wave building. Do NOT advance it further — `avg_travel_time_sec` is
    cumulative since t=0, so a clone run past the wave is dominated by history
    and stops responding to anything."""
    with engine.world_lock:
        return engine.generator.clone()


def side_for(engine, gen, impact=None, horizon=3600):
    copy = gen.clone()
    if impact is not None:
        copy.inject("weather", {"impact": impact}, source="whatif")
    return summarise_side(run_forward(copy, horizon, 60, 300, critical_map(engine)), copy.types)


def test_no_weather_modifier_is_exactly_a_no_op(client):
    """Regression protection for every pre-existing test: the new `eff` keys must
    not perturb a run with no weather modifier."""
    engine = get_engine()
    gen = warm_clone(engine)
    assert side_for(engine, gen) == side_for(engine, gen)
    identity = WeatherImpactModel({}).impact(None)
    # Injecting the identity vector changes nothing measurable either.
    assert side_for(engine, gen, identity) == side_for(engine, gen)


def test_rain_slows_travel_lengthens_queues_and_loads_the_roads(client, model):
    engine = get_engine()
    gen = warm_clone(engine)
    base = side_for(engine, gen)
    rain = side_for(engine, gen, model.impact(conditions(rain_mm_per_hr=25.0, temp_c=29.0)))

    assert rain["avg_travel_time_sec"] > base["avg_travel_time_sec"] * 1.15
    assert rain["queued_people"] > base["queued_people"]
    assert rain["road_pressure"] > base["road_pressure"]
    assert rain["venue_pressure"] > base["venue_pressure"]
    # Higher pressure on more of the network: the cascade the task asks for.
    assert rain["critical_count"] >= base["critical_count"]


def test_severity_orders_the_outcome(client, model):
    engine = get_engine()
    gen = warm_clone(engine)
    travel = []
    for rate in (0.0, 3.0, 12.0, 30.0):
        travel.append(side_for(engine, gen, model.impact(conditions(rain_mm_per_hr=rate, temp_c=29.0)))
                      ["avg_travel_time_sec"])
    assert travel == sorted(travel), travel


def test_extreme_heat_raises_emergency_facility_load(client, model):
    """Heat's signature effect, and a genuine second-order one: crowding upstream
    drives incident load through `evacuates_to`, and heat raises the gain."""
    engine = get_engine()
    gen = warm_clone(engine)

    def emergency_peak(impact):
        copy = gen.clone()
        if impact:
            copy.inject("weather", {"impact": impact}, source="whatif")
        run = run_forward(copy, 3600, 60, 300, critical_map(engine))
        posts = [u for e, u in run["peaks"].items() if copy.types[e] == "emergency_facility"]
        return max(posts)

    hot = model.impact(conditions(rain_mm_per_hr=0.0, temp_c=44.0))
    assert hot["emergency_gain_mult"] > 1.0
    assert emergency_peak(hot) > emergency_peak(None)


def test_flooding_a_named_road_propagates_to_connected_entities(client, model):
    engine = get_engine()
    gen = warm_clone(engine)
    roads = [e for e, n in engine.store.nodes.items() if n["entity_type"] == "road"]
    assert roads
    base = side_for(engine, gen)
    flood = side_for(engine, gen, model.impact(conditions(rain_mm_per_hr=30.0), flood_severity=0.9,
                                              flooded_entity_ids=roads[:2]))

    assert flood["transport_pressure"] > base["transport_pressure"]
    assert flood["avg_travel_time_sec"] > base["avg_travel_time_sec"]
    assert flood["critical_count"] > base["critical_count"]


# --- D. what-if: the isolation property, which is the whole safety case ------------------------------
def test_weather_whatif_never_mutates_the_live_city(client):
    """The core acceptance test. Mirrors
    `test_hardening.test_whatif_never_mutates_the_live_city` for weather."""
    engine = get_engine()
    with engine.world_lock:
        before = (engine.generator.utilisation(), engine.generator.elapsed_sec(),
                  [m["modifier_id"] for m in engine.generator.modifiers()],
                  engine.generator.flow_state(), engine.generator.stats())
    live_states = engine.store.snapshot_states()

    result = SIMULATIONS.run_sync(engine, [
        {"scenario_type": "weather_scenario",
         "params": {"rain_mm_per_hr": 40.0, "temp_c": 41.0, "wind_kph": 75.0,
                    "flood_severity": 0.9, "storm_duration_min": 120}},
    ], 1800, "storm")

    with engine.world_lock:
        after = (engine.generator.utilisation(), engine.generator.elapsed_sec(),
                 [m["modifier_id"] for m in engine.generator.modifiers()],
                 engine.generator.flow_state(), engine.generator.stats())
    assert after == before, "a what-if must not touch the live city"
    assert engine.store.snapshot_states() == live_states
    assert not any(m["source"] == "whatif" for m in engine.generator.modifiers())
    # And it still produced a real answer.
    assert result["baseline"] != result["scenario"]


def test_baseline_is_unchanged_while_the_scenario_moves(client):
    engine = get_engine()
    calm = SIMULATIONS.run_sync(engine, [{"scenario_type": "weather_scenario",
                                          "params": {"rain_mm_per_hr": 0.0, "temp_c": 29.0}}], 1800, "calm")
    storm = SIMULATIONS.run_sync(engine, [{"scenario_type": "weather_scenario",
                                           "params": {"rain_mm_per_hr": 30.0, "temp_c": 29.0}}], 1800, "storm")

    assert calm["baseline"] == storm["baseline"], "the do-nothing side must be identical"
    assert storm["scenario"]["avg_travel_time_sec"] > calm["scenario"]["avg_travel_time_sec"]
    assert storm["delta"]["metrics"]["avg_travel_time_sec"] > 0


def test_the_same_weather_scenario_twice_gives_the_same_answer(client):
    engine = get_engine()
    params = {"scenario_type": "weather_scenario",
              "params": {"rain_mm_per_hr": 22.0, "temp_c": 33.0, "wind_kph": 40.0}}
    first = SIMULATIONS.run_sync(engine, [params], 1800, "a")
    second = SIMULATIONS.run_sync(engine, [params], 1800, "b")
    assert first["baseline"] == second["baseline"]
    assert first["scenario"] == second["scenario"]
    assert first["weather"]["impact"] == second["weather"]["impact"]


def test_weather_whatif_reports_an_outcome_band_from_three_runs(client):
    engine = get_engine()
    result = SIMULATIONS.run_sync(engine, [{"scenario_type": "weather_scenario",
                                            "params": {"rain_mm_per_hr": 25.0, "temp_c": 30.0}}], 1800, "band")
    block = result["weather"]
    assert block is not None and block["impact"]["applied"] is True
    assert block["impact"]["model_kind"] == "rule_based"
    uncertainty = block["uncertainty"]
    assert uncertainty is not None
    assert "three runs" in uncertainty["method"]
    # Every reported metric's band must CONTAIN its own central value. An earlier
    # version selected variant scalars by band edge, which mixed a mild travel
    # coefficient with a severe service one and produced a value outside its band.
    for name, metric in uncertainty["metrics"].items():
        assert metric["lower"] <= metric["value"] <= metric["upper"], (name, metric)
    travel = uncertainty["metrics"]["avg_travel_time_sec"]
    assert travel["lower"] < travel["upper"], "the band must come from genuinely different runs"
    # The live reading the scenario was layered on is reported, so a judge can
    # see which numbers were real and which were the operator's.
    assert block["live_reading"]["availability"] in ("live", "cached", "synthetic", "unavailable")
    assert block["scenario_conditions"]["precipitation_mm_per_hr"] == 25.0


def test_a_mild_variant_is_coherently_milder_than_a_severe_one(client):
    """Each variant takes every scalar from the SAME coefficient extreme, so a
    variant is a self-consistent world rather than a mix of best and worst cases."""
    service = get_engine().weather
    bands = service.scenario_band_impacts({"rain_mm_per_hr": 25.0, "temp_c": 36.0})
    assert bands is not None
    mild, severe = bands["mild"], bands["severe"]

    # Growing multipliers: milder is smaller.
    assert mild["travel_time_mult"] < severe["travel_time_mult"]
    assert mild["dwell_mult"] < severe["dwell_mult"]
    assert mild["emergency_gain_mult"] <= severe["emergency_gain_mult"]
    assert mild["arrival_shift_min"] < severe["arrival_shift_min"]
    # Shrinking multipliers: milder is LARGER (less is lost). This is the
    # relationship the band-edge selection got backwards.
    assert mild["service_rate_mult"] > severe["service_rate_mult"]
    assert mild["attendance_mult"] >= severe["attendance_mult"]
    assert mild["arrival_spread_mult"] > severe["arrival_spread_mult"]
    for etype, mult in severe["type_capacity_mult"].items():
        assert mild["type_capacity_mult"][etype] >= mult, etype


def test_a_scenario_layers_on_the_live_reading_rather_than_a_default(client):
    engine = get_engine()
    current = engine.weather.state["current"]
    # Change only the rain; the temperature must stay the real observed one.
    scenario = engine.weather.scenario_conditions({"rain_mm_per_hr": 18.0})
    assert scenario["precipitation_mm_per_hr"] == 18.0
    assert scenario["temp_c"] == current["temp_c"]
    assert scenario["wind_kph"] == current["wind_kph"]


def test_weather_combines_with_a_non_weather_scenario(client):
    engine = get_engine()
    gate = next(e for e, n in engine.store.nodes.items() if n["entity_type"] == "gate")
    result = SIMULATIONS.run_sync(engine, [
        {"scenario_type": "weather_scenario", "params": {"rain_mm_per_hr": 25.0}},
        {"scenario_type": "gate_closure", "params": {"entity_id": gate}},
    ], 1800, "both")
    assert result["weather"] is not None
    assert result["scenario"]["avg_travel_time_sec"] > result["baseline"]["avg_travel_time_sec"]


def test_a_non_weather_whatif_carries_no_weather_block(client):
    engine = get_engine()
    gate = next(e for e, n in engine.store.nodes.items() if n["entity_type"] == "gate")
    result = SIMULATIONS.run_sync(engine, [{"scenario_type": "gate_closure",
                                            "params": {"entity_id": gate}}], 900, "gate")
    assert result["weather"] is None, "only weather scenarios should pay for the extra runs"


# --- E. driving the live city: opt-in, reversible, reaches the twin's model ---------------------------
def test_weather_is_advisory_until_an_operator_applies_it(client):
    engine = get_engine()
    assert engine.weather.driving_live is False
    payload = engine.weather.payload()
    assert payload["driving_live"] is False and payload["applied_to_live"] is False
    # Advisory means fetched and reported, not ignored.
    assert payload["current"] is not None
    assert not any(m["source"] == "weather" for m in engine.generator.modifiers())


def test_applying_weather_changes_the_live_city_and_clearing_restores_it(client, model):
    """REAL WEATHER INPUT → DIGITAL TWIN STATE CHANGE → CASCADING CHANGE, then
    back. The whole chain, on the live engine."""
    engine = get_engine()
    service = engine.weather
    # A severe reading, injected through the provider — not written into the city.
    W.set_weather_provider(W.SyntheticWeather(seed=42, base={"rain_mm_per_hr": 26.0, "temp_c": 30.0,
                                                             "wind_kph": 12.0}))
    try:
        service.refresh_sync()
        assert service.impact["applied"] is True
        assert service.impact["severity"] in ("severe", "extreme")
        before = dict(engine.generator.utilisation())
        before_travel = engine.generator.stats()["current_travel_time_sec"]

        _await(service.set_driving_live(True))
        assert service.driving_live is True
        modifiers = [m for m in engine.generator.modifiers() if m["source"] == "weather"]
        assert len(modifiers) == 1 and modifiers[0]["kind"] == "weather"
        assert service.payload()["applied_to_live"] is True

        _run(engine, 8)
        after = engine.generator.utilisation()
        assert after != before, "the live twin state must actually change"
        assert engine.generator.stats()["current_travel_time_sec"] > before_travel

        # Reversible: clearing removes the modifier entirely.
        _await(service.set_driving_live(False))
        assert not any(m["source"] == "weather" for m in engine.generator.modifiers())
        assert service.payload()["applied_to_live"] is False
    finally:
        W.set_weather_provider(W.SyntheticWeather(seed=42))
        service.refresh_sync()
        _await(service.set_driving_live(False))


def test_an_applied_forecast_also_reaches_the_twins_process_model(client):
    """A forecast is announced information, so the nominal world knows it — the
    EnKF then only corrects the gap between forecast and sensor, which is what
    keeps fidelity from collapsing when the weather turns."""
    engine = get_engine()
    service = engine.weather
    W.set_weather_provider(W.SyntheticWeather(seed=42, base={"rain_mm_per_hr": 26.0, "temp_c": 30.0}))
    try:
        service.refresh_sync()
        _await(service.set_driving_live(True))
        assert any(m["source"] == "weather" for m in engine.nominal.modifiers())
        # A fresh nominal clone keeps it too (the clone's source filter allows it).
        fresh = engine.generator.clone(sources={"intervention", "schedule", "weather"})
        assert any(m["source"] == "weather" for m in fresh.modifiers())
    finally:
        _await(service.set_driving_live(False))
        W.set_weather_provider(W.SyntheticWeather(seed=42))
        service.refresh_sync()


def test_a_counterfactual_keeps_the_weather_it_was_forked_under(client):
    """A do-nothing counterfactual answers "what if we had not acted", not
    "what if it had not rained" — so it must carry the same conditions."""
    engine = get_engine()
    service = engine.weather
    W.set_weather_provider(W.SyntheticWeather(seed=42, base={"rain_mm_per_hr": 26.0, "temp_c": 30.0}))
    try:
        service.refresh_sync()
        _await(service.set_driving_live(True))
        with engine.world_lock:
            fork = engine.generator.clone()
        assert any(m["source"] == "weather" for m in fork.modifiers())
    finally:
        _await(service.set_driving_live(False))
        W.set_weather_provider(W.SyntheticWeather(seed=42))
        service.refresh_sync()


def test_refresh_does_not_reconcile_when_nothing_material_changed(client):
    engine = get_engine()
    engine.weather.refresh_sync()
    assert engine.weather.refresh_sync() is False, "an unchanged reading is not a change"


# --- F. the API surface ---------------------------------------------------------------------------
def test_weather_endpoint_validates_and_states_its_provenance(client):
    body = client.get(f"{API}/weather").json()
    S.WeatherResponse(**body)

    assert body["availability"] in ("live", "cached", "synthetic", "unavailable")
    assert body["driving_live"] is False
    assert body["world_id"] and body["refresh_interval_sec"] > 0
    assert body["location"]["lat"] is not None
    assert body["impact"]["model_kind"] == "rule_based"
    assert [link["stage"] for link in body["impact"]["causal_chain"]][:2] == ["weather", "behaviour"]
    assert body["forecast"] and body["forecast_impacts"]


def test_weather_refresh_and_apply_endpoints(client):
    refreshed = client.post(f"{API}/weather/refresh")
    assert refreshed.status_code == 200
    S.WeatherResponse(**refreshed.json())

    on = client.post(f"{API}/weather/apply", json={"enabled": True, "operator_id": "op_test"})
    assert on.status_code == 200 and on.json()["driving_live"] is True
    off = client.post(f"{API}/weather/apply", json={"enabled": False})
    assert off.status_code == 200 and off.json()["driving_live"] is False
    assert not any(m["source"] == "weather" for m in get_engine().generator.modifiers())


def test_health_reports_the_weather_and_social_sources_honestly(client):
    body = client.get(f"{API}/health").json()
    S.HealthResponse(**body)
    weather = body["modules"]["weather"]
    assert weather["active_source"] == "synthetic"        # the module-scoped test provider
    assert "driving_live=false" in weather["detail"]
    assert body["modules"]["social"]["active_source"] == "fixture"
    assert "never presented as real" in body["modules"]["social"]["detail"]


def test_a_weather_scenario_is_accepted_through_the_real_endpoint(client):
    accepted = client.post(f"{API}/simulate", json={
        "scenarios": [{"scenario_type": "weather_scenario",
                       "params": {"rain_mm_per_hr": 25.0, "temp_c": 30.0, "storm_duration_min": 90}}],
        "horizon_sec": 1800, "label": "Heavy rain"})
    assert accepted.status_code == 202, accepted.text
    body = client.get(f"{API}/simulate/{accepted.json()['simulation_id']}").json()
    assert body["status"] == "complete", body
    S.SimulationResult(**body)
    assert body["weather"]["impact"]["applied"] is True


@pytest.mark.parametrize("params,reason", [
    ({}, "needs at least one parameter"),
    ({"rain_mm_per_hr": 500.0}, "between"),
    ({"temp_c": -90.0}, "between"),
    ({"flood_severity": 3.0}, "between"),
    ({"rain_mm_per_hr": "lots"}, "must be a number"),
    ({"rainfall": 20.0}, "Unknown weather parameter"),
    ({"flooded_entity_ids": ["no_such_entity"]}, "not an entity"),
])
def test_weather_scenario_validation_rejects_input_it_cannot_honour(client, params, reason):
    """The original `weather_rain` accepted any `intensity` string and silently
    fell back to "moderate". A rejected scenario is better than a result that
    did not simulate what was asked."""
    r = client.post(f"{API}/simulate", json={"scenarios": [{"scenario_type": "weather_scenario",
                                                            "params": params}], "horizon_sec": 1800})
    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == "INVALID_SCENARIO"
    assert reason in r.json()["error"]["message"]


def test_the_original_coarse_rain_scenario_still_works(client):
    """`weather_rain` predates this feature and other tests / mocks rely on it."""
    r = client.post(f"{API}/simulate", json={
        "scenarios": [{"scenario_type": "weather_rain", "params": {"intensity": "heavy"}}],
        "horizon_sec": 900})
    assert r.status_code == 202
    body = client.get(f"{API}/simulate/{r.json()['simulation_id']}").json()
    assert body["status"] == "complete" and body["weather"] is None


def test_applying_weather_as_a_live_disruption_uses_the_impact_vector(client):
    engine = get_engine()
    created = client.post(f"{API}/disruptions", json={
        "scenario_type": "weather_scenario", "params": {"rain_mm_per_hr": 20.0},
        "label": "Heavy rain"}).json()
    try:
        # The record keeps the operator's own parameters…
        assert created["params"]["rain_mm_per_hr"] == 20.0
        # …while the city got the translated vector.
        applied = [m for m in engine.generator.modifiers() if m["modifier_id"] == created["disruption_id"]]
        assert len(applied) == 1 and applied[0]["kind"] == "weather"
        assert applied[0]["params"]["impact"]["travel_time_mult"] > 1.0
    finally:
        client.delete(f"{API}/disruptions/{created['disruption_id']}")
    assert not any(m["modifier_id"] == created["disruption_id"] for m in engine.generator.modifiers())


# --- G. public social signals ----------------------------------------------------------------------
def social_provider(responses, by_tag=None, max_tags=2):
    """`responses` is served in call order; `by_tag` (optional) serves a specific
    timeline per hashtag, which is how a real instance behaves — a post found
    under `#rain` is not evidence that it is about our venue."""
    state = {"i": 0, "tags": []}

    def transport(method, url, headers, body, timeout):
        tag = url.split("/tag/")[-1].split("?")[0]
        state["tags"].append(tag)
        item = (by_tag or {}).get(tag) if by_tag is not None else None
        if item is None:
            item = responses[min(state["i"], len(responses) - 1)] if responses else []
        state["i"] += 1
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, int):
            return item, b"{}"
        return 200, json.dumps(item).encode()

    client = HttpClient(user_agent="test", timeout_sec=0.2, retries=0, cache_ttl_sec=0.0,
                        transport=transport)
    return SOC.MastodonPublicSignals(client=client, max_tags=max_tags), state


def status(text, *, sid="1", when="2026-09-04T12:00:00.000Z", acct="someone@example.social"):
    return {"id": sid, "created_at": when, "url": f"https://example.social/@x/{sid}",
            "content": f"<p>{text}</p>", "account": {"acct": acct}}


def test_real_posts_are_classified_and_attributed():
    provider, _ = social_provider([[
        status("Heavy <a href='#'>#<span>rain</span></a> at the stadium, roads flooded", sid="11"),
        status("Nice day for a walk", sid="12"),                     # no weather term: dropped
    ]])
    result = provider.signals(["Test Stadium"], window_sec=10 ** 9, limit=10)

    assert result["availability"] == "live"
    assert len(result["signals"]) == 1
    signal = result["signals"][0]
    # Inline markup must not be turned into spaces: "#rain", not "# rain".
    assert "#rain" in signal["text"] and "flooded" in signal["text"]
    assert signal["is_real_post"] is True
    assert signal["observation_kind"] == "user_report"       # the platform's data
    assert signal["classification_kind"] == "derived"        # our label
    assert signal["signal_type"] == "flood_report"
    assert signal["author"] == "@someone@example.social" and signal["url"]
    assert signal["posted_at"].endswith("Z")
    assert result["classifier"] == "keyword_match_v1"


def test_a_distant_weather_post_is_global_not_local():
    """`#rain` returns weather posts from everywhere. "Is about weather" and "is
    about *here*" are different questions, and the payload answers both."""
    provider, state = social_provider([], by_tag={
        "teststadium": [status("Rain over the stadium, queues building", sid="22")],
        "rain": [status("Flooding in Bangkok tonight", sid="21")],
    })
    result = provider.signals(["Test Stadium"], window_sec=10 ** 9, limit=10)

    assert state["tags"] == ["teststadium", "rain"]
    assert {s["scope"] for s in result["signals"]} == {"local", "global"}
    assert result["summary"]["local_count"] == 1 and result["summary"]["global_count"] == 1
    # Local signals rank first and score higher; distant ones are kept, not faked away.
    assert result["signals"][0]["scope"] == "local"
    assert result["signals"][0]["relevance"] > result["signals"][-1]["relevance"]
    # A post found under the venue's own hashtag counts as local whatever it says.
    assert result["signals"][0]["location_topic"] == "#teststadium"


def test_an_empty_result_is_a_real_answer_not_a_failure():
    provider, _ = social_provider([[]])
    result = provider.signals(["Test Stadium"], limit=10)
    assert result["availability"] == "live"
    assert result["signals"] == [] and result["summary"]["count"] == 0
    assert "No public post" in result["detail"]


@pytest.mark.parametrize("failure", [OSError("down"), TimeoutError("slow"), 429, 500])
def test_social_provider_failure_is_reported_never_invented(failure):
    provider, _ = social_provider([failure])
    result = provider.signals(["Test Stadium"], limit=5)
    assert result["availability"] == "unavailable"
    assert result["signals"] == []
    assert result["errors"] and result["detail"]


def test_social_falls_back_to_the_last_good_set_when_every_tag_fails():
    provider, _ = social_provider([[status("Heavy rain here", sid="31")], OSError("down")])
    first = provider.signals(["Test Stadium"], window_sec=10 ** 9, limit=5)
    assert first["availability"] == "live" and first["signals"]
    second = provider.signals(["Test Stadium"], window_sec=10 ** 9, limit=5)
    assert second["availability"] == "cached"
    assert second["signals"] == first["signals"] and second["age_sec"] is not None


def test_one_dead_hashtag_does_not_blank_the_panel():
    provider, _ = social_provider([[status("Flooded underpass", sid="41")], 500])
    result = provider.signals(["Test Stadium"], window_sec=10 ** 9, limit=5)
    assert result["availability"] == "live" and result["signals"]
    assert result["errors"] and len(result["errors"]) == 1


def test_posts_outside_the_window_are_dropped():
    provider, _ = social_provider([[status("Heavy rain", sid="51", when="2020-01-01T00:00:00.000Z")]])
    assert provider.signals(["Test Stadium"], window_sec=3600, limit=5)["signals"] == []


def test_fixture_signals_are_never_presented_as_real_posts():
    result = SOC.FixtureSignals().signals(["Test City"], window_sec=21600, limit=10)
    assert result["availability"] == "fixture"
    assert result["signals"]
    for signal in result["signals"]:
        assert signal["is_real_post"] is False
        assert signal["source"] == "fallback_fixture"
        assert signal["author"] is None and signal["url"] is None
        assert signal["observation_kind"] == "synthetic_test_data"
    assert "No real social posts" in result["detail"]


def test_social_endpoint_validates_and_names_the_active_world(client):
    body = client.get(f"{API}/social/signals?window_sec=21600&limit=10").json()
    S.SocialSignalsResponse(**body)
    assert body["provider"] == "fixture"
    assert body["topics"], "topics must come from the active world, not a constant"
    assert all(not s["is_real_post"] for s in body["signals"])


def test_social_provider_selection():
    assert SOC.build_social_provider({"provider": "none"}, env={}).name == "none"
    assert SOC.build_social_provider({"provider": "fixture"}, env={}).name == "fixture"
    assert SOC.build_social_provider({}, env={}).name == "mastodon"
    assert SOC.build_social_provider({}, env={"EVENTFLOW_SOCIAL_PROVIDER": "none"}).name == "none"


def test_hashtags_are_derived_from_the_world_never_hardcoded(client):
    assert SOC.hashtag("Narendra Modi Stadium") == "narendramodistadium"
    assert SOC.hashtag("a") is None and SOC.hashtag(None) is None
    engine = get_engine()
    topics = engine.weather.topics()
    assert topics and all(isinstance(t, str) for t in topics)
    # The venue of the ACTIVE world is among them.
    venue = engine.store.nodes.get(engine.world.get("venue_entity_id") or "")
    if venue:
        assert venue["display_name"] in topics


def test_post_text_is_stripped_and_capped():
    long_text = "rain " * 400
    provider, _ = social_provider([[status(f"<b>{long_text}</b><script>bad()</script>", sid="61")]])
    signal = provider.signals(["Test Stadium"], window_sec=10 ** 9, limit=5)["signals"][0]
    assert len(signal["text"]) <= SOC.MAX_TEXT_CHARS
    assert "<" not in signal["text"] and "script" not in signal["text"]


# --- H. the weather layer follows the active world -------------------------------------------------
def test_the_weather_location_is_the_active_worlds_venue(client):
    engine = get_engine()
    location = engine.weather.location()
    venue_id = engine.world.get("venue_entity_id")
    venue = engine.store.nodes[venue_id]
    assert location["lat"] == pytest.approx(venue["lat"])
    assert location["lon"] == pytest.approx(venue["lon"])
    assert location["entity_id"] == venue_id
    # Nothing in the weather layer hardcodes a city.
    assert not math.isclose(location["lat"], 0.0)
