"""Weather provider: current conditions and a short forecast for one location.

The default provider is **Open-Meteo** (`https://api.open-meteo.com`), which
needs no API key and no account, so the core demo runs against real live
weather out of the box. A different source can be switched on without code
changes:

    EVENTFLOW_WEATHER_PROVIDER=open_meteo   (default)
    EVENTFLOW_WEATHER_PROVIDER=synthetic    deterministic, offline, for tests
    EVENTFLOW_WEATHER_PROVIDER=none         weather layer reports "unavailable"

Honesty rules this module exists to enforce (the task is explicit about them):

* Every reading carries `availability`: `"live"` (just fetched from the real
  API), `"cached"` (the API is failing, this is the last good reading and
  `age_sec` says how old it is), `"synthetic"` (a deterministic offline model,
  never presented as an observation) or `"unavailable"` (no data at all —
  `current` is `null` and `detail` says why).
* A fallback is **never** relabelled as live. `SyntheticWeather` is only used
  when it is explicitly configured, not as a silent rescue for a failed fetch.
* Network failure, timeout, HTTP error, malformed JSON and a missing forecast
  block are all handled and all reported. Nothing raises into the cycle.
* `stale` is true once a cached reading is older than `stale_after_sec`, so the
  UI can say "this is old" rather than quietly showing yesterday's rain.

Units follow 00_SHARED_CONTRACT.md §0: durations are integer seconds with a
`_sec` suffix, fractions (humidity, precipitation probability) are floats
0.0–1.0, timestamps are ISO 8601 UTC with a `Z`. Temperature is `_c`
(degrees Celsius) and rainfall is `_mm_per_hr` — both additive units, declared
here and in `01_BACKEND_CONTRACT.md`.
"""
from __future__ import annotations

import logging
import math
import threading
import time
from datetime import datetime, timezone
from typing import Any, Protocol

from ..geospatial.http import HttpClient, ProviderError
from ..ml_reference.common import clamp

log = logging.getLogger("eventflow.weather")

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

CURRENT_FIELDS = (
    "temperature_2m", "relative_humidity_2m", "apparent_temperature", "precipitation",
    "rain", "weather_code", "wind_speed_10m", "wind_gusts_10m",
)
HOURLY_FIELDS = (
    "temperature_2m", "apparent_temperature", "precipitation", "precipitation_probability",
    "weather_code", "wind_speed_10m",
)

# WMO 4677 present-weather codes, as documented by Open-Meteo. Used for the
# human-readable `condition` label only; every numeric impact comes from the
# measured values, never from the code.
WMO_CONDITIONS: dict[int, str] = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "depositing rime fog",
    51: "light drizzle", 53: "moderate drizzle", 55: "dense drizzle",
    56: "light freezing drizzle", 57: "dense freezing drizzle",
    61: "slight rain", 63: "moderate rain", 65: "heavy rain",
    66: "light freezing rain", 67: "heavy freezing rain",
    71: "slight snowfall", 73: "moderate snowfall", 75: "heavy snowfall", 77: "snow grains",
    80: "slight rain showers", 81: "moderate rain showers", 82: "violent rain showers",
    85: "slight snow showers", 86: "heavy snow showers",
    95: "thunderstorm", 96: "thunderstorm with slight hail", 99: "thunderstorm with heavy hail",
}
# Codes that are themselves a storm, whatever the measured rate happens to be
# in the reporting interval.
STORM_CODES = {95, 96, 99, 82}

# Severity thresholds. The rainfall bands are the standard meteorological
# hourly rain-rate classes (light < 2.5, moderate 2.5–7.6, heavy 7.6–30,
# violent > 30 mm/hr); the wind bands are Beaufort 6 / 8 / 10 in km/h. The
# apparent-temperature bands are heat-stress estimates, not a clinical index.
DEFAULT_SEVERITY = {
    "rain_mm_per_hr": {"moderate": 2.5, "severe": 7.6, "extreme": 30.0},
    "wind_kph": {"moderate": 39.0, "severe": 62.0, "extreme": 88.0},
    "apparent_c": {"moderate": 35.0, "severe": 40.0, "extreme": 45.0},
}
SEVERITY_ORDER = ("calm", "moderate", "severe", "extreme")


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_api_time(value: str | None) -> datetime | None:
    """Open-Meteo with `timezone=UTC` returns `2026-09-27T08:00` (no zone)."""
    if not value:
        return None
    raw = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _number(value: Any) -> float | None:
    """A provider may legitimately send `null` for a field it has no data for.
    `null` means unknown (00 §0) and must never become 0.0."""
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(out) or math.isinf(out) else out


def severity_of(conditions: dict[str, Any], thresholds: dict[str, dict[str, float]] | None = None) -> str:
    """Worst of the rainfall / wind / heat bands (`calm` → `extreme`)."""
    th = thresholds or DEFAULT_SEVERITY
    worst = 0
    for key, field in (("rain_mm_per_hr", "precipitation_mm_per_hr"), ("wind_kph", "wind_kph"),
                       ("apparent_c", "apparent_temp_c")):
        value = conditions.get(field)
        if value is None:
            continue
        band = th.get(key, DEFAULT_SEVERITY[key])
        level = 0
        for index, name in enumerate(SEVERITY_ORDER[1:], start=1):
            if float(value) >= float(band.get(name, DEFAULT_SEVERITY[key][name])):
                level = index
        worst = max(worst, level)
    if conditions.get("condition_code") in STORM_CODES:
        worst = max(worst, 2)   # a reported thunderstorm is at least "severe"
    return SEVERITY_ORDER[worst]


def build_conditions(*, valid_at: str, temp_c: float | None, apparent_temp_c: float | None,
                     precipitation_mm_per_hr: float | None, wind_kph: float | None,
                     humidity: float | None = None, precipitation_probability: float | None = None,
                     condition_code: int | None = None, horizon_sec: int = 0,
                     thresholds: dict | None = None) -> dict[str, Any]:
    """One normalised weather reading. Every documented key is always present;
    a value the provider did not supply is `null`, never a sentinel."""
    if apparent_temp_c is None:
        apparent_temp_c = temp_c
    out = {
        "valid_at": valid_at,
        "horizon_sec": int(horizon_sec),
        "temp_c": None if temp_c is None else round(float(temp_c), 2),
        "apparent_temp_c": None if apparent_temp_c is None else round(float(apparent_temp_c), 2),
        "precipitation_mm_per_hr": None if precipitation_mm_per_hr is None
        else round(max(0.0, float(precipitation_mm_per_hr)), 3),
        "wind_kph": None if wind_kph is None else round(max(0.0, float(wind_kph)), 2),
        "humidity": None if humidity is None else round(clamp(float(humidity), 0.0, 1.0), 4),
        "precipitation_probability": None if precipitation_probability is None
        else round(clamp(float(precipitation_probability), 0.0, 1.0), 4),
        "condition_code": None if condition_code is None else int(condition_code),
        "condition": WMO_CONDITIONS.get(int(condition_code), "unknown") if condition_code is not None else None,
    }
    out["severity"] = severity_of(out, thresholds)
    return out


def unavailable(location: dict[str, Any], detail: str, provider: str) -> dict[str, Any]:
    """A weather state with no reading in it. The impact layer applies nothing
    and the UI says so — this is never dressed up as a calm day."""
    return {
        "provider": provider, "availability": "unavailable", "source": f"{provider}:unavailable",
        "fetched_at": _iso(datetime.now(timezone.utc)), "observed_at": None, "age_sec": None,
        "stale": True, "location": dict(location), "current": None, "forecast": [], "detail": detail,
    }


class WeatherProvider(Protocol):
    name: str

    def observe(self, lat: float, lon: float, label: str | None = None) -> dict[str, Any]:
        """-> a weather state dict (see `unavailable` for the key set)."""
        ...


class SyntheticWeather:
    """Deterministic offline weather, for tests and for running with no network.

    It is a smooth function of the requested location and the hour — a
    plausible shape, **not an observation**. Its `availability` is always
    `"synthetic"` so nothing downstream can mistake it for live data.
    """

    name = "synthetic"

    def __init__(self, seed: int = 42, thresholds: dict | None = None,
                 clock: Any = None, base: dict[str, float] | None = None) -> None:
        self.seed = int(seed)
        self.thresholds = thresholds
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._base = dict(base or {})

    def _phase(self, lat: float, lon: float) -> float:
        # Stable per-location offset so two venues do not share one weather curve.
        return ((abs(lat) * 7.31 + abs(lon) * 3.17) % 1.0) * 2.0 * math.pi

    def _at(self, phase: float, hour: int) -> dict[str, float]:
        wave = math.sin(phase + hour / 24.0 * 2.0 * math.pi)
        return {
            "temp_c": round(self._base.get("temp_c", 29.0) + 5.0 * wave, 2),
            "rain": round(max(0.0, self._base.get("rain_mm_per_hr", 0.6) + 1.8 * math.sin(phase + hour / 6.0)), 3),
            "wind": round(max(0.0, self._base.get("wind_kph", 12.0) + 6.0 * math.cos(phase + hour / 8.0)), 2),
            "humidity": round(clamp(0.55 + 0.2 * wave, 0.0, 1.0), 4),
        }

    def observe(self, lat: float, lon: float, label: str | None = None) -> dict[str, Any]:
        now = self._clock()
        top_of_hour = now.replace(minute=0, second=0, microsecond=0)
        phase = self._phase(lat, lon)

        def point(step: int) -> dict[str, Any]:
            v = self._at(phase, (now.hour + step) % 24)
            valid = top_of_hour if step == 0 else datetime.fromtimestamp(
                top_of_hour.timestamp() + step * 3600, tz=timezone.utc)
            return build_conditions(
                valid_at=_iso(valid), temp_c=v["temp_c"], apparent_temp_c=round(v["temp_c"] + 1.5, 2),
                precipitation_mm_per_hr=v["rain"], wind_kph=v["wind"], humidity=v["humidity"],
                precipitation_probability=round(clamp(v["rain"] / 10.0, 0.0, 1.0), 4),
                condition_code=61 if v["rain"] >= 0.5 else 2,
                horizon_sec=step * 3600, thresholds=self.thresholds,
            )

        forecast = [point(step) for step in range(1, 13)]
        current = point(0)
        current["valid_at"] = _iso(now)
        return {
            "provider": self.name, "availability": "synthetic", "source": "synthetic",
            "fetched_at": _iso(now), "observed_at": current["valid_at"], "age_sec": 0, "stale": False,
            "location": {"lat": round(float(lat), 5), "lon": round(float(lon), 5), "label": label},
            "current": current, "forecast": forecast,
            "detail": "Deterministic offline model — a plausible shape, not an observation.",
        }


class NoWeather:
    """Weather explicitly switched off. Everything downstream reports unavailable."""

    name = "none"

    def observe(self, lat: float, lon: float, label: str | None = None) -> dict[str, Any]:
        return unavailable({"lat": round(float(lat), 5), "lon": round(float(lon), 5), "label": label},
                           "Weather integration is disabled in configuration.", self.name)


class OpenMeteoWeather:
    """Live current conditions + hourly forecast from Open-Meteo (no API key).

    On any failure the last good reading is returned with
    `availability="cached"`; with no reading yet, `availability="unavailable"`.
    """

    name = "open_meteo"

    def __init__(self, client: HttpClient | None = None, forecast_hours: int = 12,
                 stale_after_sec: float = 3600.0, thresholds: dict | None = None,
                 url: str = OPEN_METEO_URL, user_agent: str = "EventFlow-AI/1.0") -> None:
        self.url = url
        self.forecast_hours = max(1, int(forecast_hours))
        self.stale_after_sec = float(stale_after_sec)
        self.thresholds = thresholds
        # Weather changes on the scale of minutes: a short client cache keeps a
        # burst of UI requests from turning into a burst of provider requests,
        # and the service layer holds the long-lived "last good" copy.
        self.client = client or HttpClient(user_agent=user_agent, timeout_sec=6.0, retries=1,
                                           cache_ttl_sec=120.0, cache_size=64)
        self._last: dict[tuple, tuple[float, dict]] = {}
        self._lock = threading.Lock()
        self.stats = {"calls": 0, "failures": 0, "cached_answers": 0}

    # --- parsing ---------------------------------------------------------------
    def _current(self, body: dict, now: datetime) -> dict[str, Any] | None:
        cur = body.get("current")
        if not isinstance(cur, dict):
            return None
        observed = _parse_api_time(cur.get("time")) or now
        rain = _number(cur.get("precipitation"))
        if rain is None:
            rain = _number(cur.get("rain"))
        humidity = _number(cur.get("relative_humidity_2m"))
        temp = _number(cur.get("temperature_2m"))
        if temp is None and rain is None:
            return None   # a `current` block with neither is not a reading
        return build_conditions(
            valid_at=_iso(observed), temp_c=temp,
            apparent_temp_c=_number(cur.get("apparent_temperature")),
            precipitation_mm_per_hr=rain,
            wind_kph=_number(cur.get("wind_speed_10m")),
            humidity=None if humidity is None else humidity / 100.0,
            condition_code=None if _number(cur.get("weather_code")) is None else int(cur["weather_code"]),
            horizon_sec=0, thresholds=self.thresholds,
        )

    def _forecast(self, body: dict, observed: datetime) -> list[dict[str, Any]]:
        """Hourly points strictly after `observed`. A missing or malformed
        `hourly` block yields an empty forecast, never an exception — an
        unavailable forecast is a documented operating state."""
        hourly = body.get("hourly")
        if not isinstance(hourly, dict) or not isinstance(hourly.get("time"), list):
            return []
        times = hourly["time"]

        def column(field: str) -> list:
            value = hourly.get(field)
            return value if isinstance(value, list) and len(value) == len(times) else [None] * len(times)

        temps, apparent = column("temperature_2m"), column("apparent_temperature")
        precip, prob = column("precipitation"), column("precipitation_probability")
        codes, winds = column("weather_code"), column("wind_speed_10m")
        out: list[dict[str, Any]] = []
        for index, stamp in enumerate(times):
            valid = _parse_api_time(stamp)
            if valid is None:
                continue
            horizon = (valid - observed).total_seconds()
            if horizon <= 0:
                continue   # already in the past relative to the observation
            probability = _number(prob[index])
            out.append(build_conditions(
                valid_at=_iso(valid), temp_c=_number(temps[index]),
                apparent_temp_c=_number(apparent[index]),
                precipitation_mm_per_hr=_number(precip[index]),
                wind_kph=_number(winds[index]),
                precipitation_probability=None if probability is None else probability / 100.0,
                condition_code=None if _number(codes[index]) is None else int(codes[index]),
                horizon_sec=int(round(horizon)), thresholds=self.thresholds,
            ))
            if len(out) >= self.forecast_hours:
                break
        return out

    # --- fetch -----------------------------------------------------------------
    def observe(self, lat: float, lon: float, label: str | None = None) -> dict[str, Any]:
        location = {"lat": round(float(lat), 5), "lon": round(float(lon), 5), "label": label}
        key = (round(float(lat), 3), round(float(lon), 3))
        now = datetime.now(timezone.utc)
        params = {
            "latitude": f"{float(lat):.4f}", "longitude": f"{float(lon):.4f}",
            "current": ",".join(CURRENT_FIELDS), "hourly": ",".join(HOURLY_FIELDS),
            "forecast_hours": self.forecast_hours + 1, "timezone": "UTC", "wind_speed_unit": "kmh",
        }
        detail: str | None = None
        try:
            self.stats["calls"] += 1
            body = self.client.request_json(self.name, "GET", self.url, params=params)
            if not isinstance(body, dict):
                raise ProviderError(self.name, "response was not a JSON object")
            current = self._current(body, now)
            if current is None:
                raise ProviderError(self.name, "response carried no current conditions")
        except ProviderError as exc:
            detail = exc.reason
        except Exception as exc:                       # never raise into the cycle
            detail = f"unexpected provider error ({type(exc).__name__})"
        else:
            observed = _parse_api_time(body["current"].get("time")) or now
            forecast = self._forecast(body, observed)
            state = {
                "provider": self.name, "availability": "live", "source": self.name,
                "fetched_at": _iso(now), "observed_at": _iso(observed),
                "age_sec": max(0, int(round((now - observed).total_seconds()))),
                "stale": False, "location": location, "current": current, "forecast": forecast,
                "detail": None if forecast else "The provider returned no hourly forecast for this location.",
            }
            with self._lock:
                self._last[key] = (time.time(), state)
            return dict(state)

        self.stats["failures"] += 1
        log.warning("weather fetch failed for %s: %s", key, detail)
        with self._lock:
            hit = self._last.get(key)
        if hit is None:
            return unavailable(location, detail or "the provider could not be reached", self.name)
        cached_at, state = hit
        age = max(0, int(round(time.time() - cached_at)))
        self.stats["cached_answers"] += 1
        return {
            **{k: v for k, v in state.items() if k not in ("availability", "source", "age_sec", "stale", "detail")},
            "availability": "cached", "source": f"cached:{self.name}",
            "age_sec": (state.get("age_sec") or 0) + age, "stale": age >= self.stale_after_sec,
            "detail": f"Live fetch failed ({detail}); showing the last good reading from {age}s ago.",
        }


_PROVIDER: WeatherProvider | None = None


def build_weather_provider(cfg: dict | None = None, env: dict | None = None,
                           client: HttpClient | None = None) -> WeatherProvider:
    """`weather.provider` in config.yaml, overridable by EVENTFLOW_WEATHER_PROVIDER."""
    import os

    cfg = dict(cfg or {})
    env = os.environ if env is None else env
    kind = (env.get("EVENTFLOW_WEATHER_PROVIDER") or cfg.get("provider") or "open_meteo").lower()
    thresholds = cfg.get("severity") or DEFAULT_SEVERITY
    if kind in ("none", "off", "disabled"):
        return NoWeather()
    if kind == "synthetic":
        # `base` lets a deterministic run stand in for a specific kind of day
        # (tests, offline demos, verifying the severe-weather UI paths).
        return SyntheticWeather(seed=int(cfg.get("seed", 42)), thresholds=thresholds,
                                base=cfg.get("base") or {})
    if kind != "open_meteo":
        log.warning("unknown weather provider %r; using open_meteo", kind)
    if client is None:
        client = HttpClient(
            user_agent=str(cfg.get("user_agent", "EventFlow-AI/1.0")),
            timeout_sec=float(cfg.get("timeout_sec", 6.0)),
            retries=int(cfg.get("retries", 1)),
            min_interval_sec={"api.open-meteo.com": float(cfg.get("min_interval_sec", 1.0))},
            cache_ttl_sec=float(cfg.get("client_cache_ttl_sec", 120.0)),
            cache_size=64,
        )
    return OpenMeteoWeather(
        client=client, forecast_hours=int(cfg.get("forecast_hours", 12)),
        stale_after_sec=float(cfg.get("stale_after_sec", 3600.0)), thresholds=thresholds,
        url=str(cfg.get("url", OPEN_METEO_URL)),
    )


def get_weather_provider() -> WeatherProvider:
    global _PROVIDER
    if _PROVIDER is None:
        from ..config import get_config

        _PROVIDER = build_weather_provider(get_config().raw.get("weather") or {})
    return _PROVIDER


def set_weather_provider(provider: WeatherProvider | None) -> None:
    """Test hook — the engine always resolves through `get_weather_provider`."""
    global _PROVIDER
    _PROVIDER = provider
