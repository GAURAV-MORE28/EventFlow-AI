"""Public social signals: real posts people published about conditions.

The live provider reads **Mastodon public hashtag timelines**
(`GET /api/v1/timelines/tag/{tag}`), which most instances serve without an API
key or an account. That is the whole reason it was chosen: it is genuinely
public, genuinely user-generated, and needs no credential to read, so the demo
shows real signals rather than a simulation of signals.

    EVENTFLOW_SOCIAL_PROVIDER=mastodon   (default)
    EVENTFLOW_SOCIAL_PROVIDER=fixture    offline, clearly labelled, for tests
    EVENTFLOW_SOCIAL_PROVIDER=none       the panel reports "unavailable"

## Rules this module exists to enforce

* **Nothing is fabricated.** There is no code path that invents a post. The
  offline provider reads a checked-in file and every signal it returns is
  labelled `source: "fallback_fixture"` with `is_real_post: false`, so it can
  never be shown as something a person wrote today.
* An empty result is a real result. If nobody posted about the weather near this
  venue, the response is `signals: []` with `availability: "live"` — the panel
  says "no signals in the window", which is the truth.
* Each signal states its **source** (instance + hashtag), **timestamp**,
  **signal type**, **location/topic**, and separates what was *observed* from
  what was *derived*: `text` / `author` / `url` / `posted_at` come from the
  platform verbatim (`observation_kind: "user_report"`), while `signal_type`,
  `relevance` and `matched_terms` are our own keyword classification
  (`classification_kind: "derived"`, `classifier` names the method).
* Hashtags are derived from the **active world's** venue and city, never
  hardcoded, so activating a blueprint for another city queries that city.

## Security note

Post text is untrusted third-party content. It is HTML-stripped, length-capped
and passed through as data only. Nothing in the backend interprets it, branches
on it, or lets it influence the simulation; the weather that drives the twin
comes from the weather provider alone. The frontend renders it as text, never
as markup.
"""
from __future__ import annotations

import html
import json
import logging
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

from ..geospatial.http import HttpClient, ProviderError

log = logging.getLogger("eventflow.social")

MASTODON_INSTANCE = "https://mastodon.social"
MAX_TEXT_CHARS = 400

# Keyword → signal type. The classification is a documented keyword match, not a
# language model: `classifier` in the payload says exactly that.
SIGNAL_TERMS: dict[str, tuple[str, ...]] = {
    "flood_report": ("flood", "flooded", "flooding", "waterlogged", "waterlogging", "inundated", "knee deep"),
    "storm_report": ("storm", "thunderstorm", "thunder", "lightning", "squall", "cyclone", "gale", "hail"),
    "rain_report": ("rain", "raining", "rainfall", "downpour", "drizzle", "monsoon", "cloudburst", "showers"),
    "heat_report": ("heatwave", "heat wave", "heatstroke", "sweltering", "scorching", "humid", "heat advisory"),
    "transport_disruption": ("delayed", "delay", "cancelled", "canceled", "diverted", "traffic jam", "gridlock",
                             "no trains", "bus stuck", "road closed", "metro delay"),
    "crowd_report": ("queue", "queuing", "crowded", "packed", "long line", "long queue", "crush", "stampede"),
}
# A post must hit at least one of these to count as weather-related at all.
WEATHER_ANCHORS = tuple(sorted({t for key, terms in SIGNAL_TERMS.items() for t in terms
                                if key != "crowd_report"}))

# Block-level markup is a line break (becomes a space); inline markup is not
# (Mastodon wraps hashtags and URL fragments in <span>, so replacing those with
# a space would split "#rain" into "# rain" and break every URL).
_BLOCK_HTML = re.compile(r"</?(?:p|br|div|li|ul|ol|blockquote|h[1-6])\b[^>]*>", re.I)
_TAG_HTML = re.compile(r"<[^>]+>")
_WHITESPACE = re.compile(r"\s+")
_NON_TAG = re.compile(r"[^0-9a-z]+")


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def strip_html(raw: Any) -> str:
    """Mastodon serves `content` as HTML. Reduce it to plain text and cap it."""
    if not isinstance(raw, str):
        return ""
    text = _TAG_HTML.sub("", _BLOCK_HTML.sub(" ", raw))
    text = _WHITESPACE.sub(" ", html.unescape(text)).strip()
    return text[:MAX_TEXT_CHARS]


def hashtag(label: str | None) -> str | None:
    """`"Narendra Modi Stadium"` -> `"narendramodistadium"`. Returns None for
    anything too short or too long to be a usable tag."""
    if not label:
        return None
    tag = _NON_TAG.sub("", str(label).lower())
    return tag if 3 <= len(tag) <= 30 else None


def classify(text: str) -> tuple[str | None, list[str]]:
    """(signal_type, matched_terms) from a keyword match. The most specific
    match wins: flooding before storm before rain, so "flooded road in the
    storm" is a flood report."""
    lowered = text.lower()
    matched: list[str] = []
    chosen: str | None = None
    for signal_type, terms in SIGNAL_TERMS.items():
        hits = [t for t in terms if t in lowered]
        if hits:
            matched.extend(hits)
            if chosen is None:
                chosen = signal_type
    return chosen, sorted(set(matched))


def matches_location(text: str, tag: str, topics: list[str]) -> bool:
    """Did this post actually name the active world's venue or city?

    A hashtag timeline for `#rain` returns weather posts from everywhere, so
    "is about weather" and "is about *here*" are separate questions and the
    payload answers both. The location tag is checked against the post text and
    against the hashtag the post was found under.
    """
    lowered = text.lower()
    wanted = [t.lower().lstrip("#") for t in topics if t]
    if tag.lower() in wanted:
        return True
    return any(topic in lowered for topic in wanted)


def relevance(text: str, matched: list[str], topics: list[str], tag: str = "") -> float:
    """A documented, reproducible score in 0.0–1.0 — not a model output.

    0.5 for naming a weather condition, +0.2 for naming more than one, +0.3 when
    the post is about this world's location (`matches_location`). A distant
    #flood post therefore tops out at 0.7 and a local one reaches 1.0, so
    sorting by relevance puts local signals first without discarding the rest.
    `classification_kind: "derived"` says this is our arithmetic, not the
    platform's.
    """
    score = 0.5 if matched else 0.0
    if len(matched) > 1:
        score += 0.2
    if matches_location(text, tag, topics):
        score += 0.3
    return round(min(1.0, score), 3)


def unavailable(detail: str, provider: str, tags: list[str], window_sec: int) -> dict[str, Any]:
    return {
        "provider": provider, "availability": "unavailable", "fetched_at": _iso(datetime.now(timezone.utc)),
        "queried_tags": list(tags), "window_sec": int(window_sec), "signals": [],
        "summary": {"count": 0, "local_count": 0, "global_count": 0, "by_type": {}, "max_relevance": None},
        "detail": detail,
        "classifier": "keyword_match_v1",
    }


class PublicSignalProvider(Protocol):
    name: str

    def signals(self, topics: list[str], *, window_sec: int = 21600, limit: int = 20) -> dict[str, Any]:
        """-> a signal set (see `unavailable` for the key set)."""
        ...


class NoSignals:
    """Social integration explicitly switched off."""

    name = "none"

    def signals(self, topics, *, window_sec=21600, limit=20):
        return unavailable("Public-signal integration is disabled in configuration.", self.name, [], window_sec)


class FixtureSignals:
    """Offline provider for tests and for running with no network.

    Every signal it returns carries `source: "fallback_fixture"` and
    `is_real_post: false`. These are **not** social media posts and are never
    presented as any person's words — they exist so the classification and UI
    paths can be exercised without the network.
    """

    name = "fixture"

    def __init__(self, path: Path | str | None = None, now: Any = None) -> None:
        self.path = Path(path) if path else Path(__file__).resolve().parent.parent / "data" / "social_fixture.json"
        self._now = now or (lambda: datetime.now(timezone.utc))

    def signals(self, topics, *, window_sec=21600, limit=20):
        now = self._now()
        tags = [t for t in (hashtag(x) for x in topics) if t]
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            entries = raw["entries"] if isinstance(raw, dict) else raw
            if not isinstance(entries, list):
                raise ValueError("fixture is not a list of entries")
        except Exception as exc:
            return unavailable(f"fixture could not be read ({type(exc).__name__})", self.name, tags, window_sec)
        out = []
        for index, entry in enumerate(entries[:limit]):
            text = strip_html(entry.get("text"))
            signal_type, matched = classify(text)
            age = int(entry.get("age_sec", 1800))
            if age > window_sec:
                continue
            out.append({
                "signal_id": f"sig_fx_{index:03d}",
                "source": "fallback_fixture",
                "source_detail": f"{self.path.name} — offline test data, not a real post",
                "is_real_post": False,
                "posted_at": _iso(now - timedelta(seconds=age)),
                "author": None, "url": None,
                "text": text,
                "location_topic": entry.get("location_topic") or (f"#{tags[0]}" if tags else None),
                "signal_type": signal_type or "general_weather",
                "observation_kind": "synthetic_test_data",
                "classification_kind": "derived",
                "matched_terms": matched,
                "scope": "local",
                "relevance": relevance(text, matched, tags, tags[0] if tags else ""),
            })
        return {
            "provider": self.name, "availability": "fixture",
            "fetched_at": _iso(now), "queried_tags": tags, "window_sec": int(window_sec),
            "signals": out, "summary": _summarise(out),
            "detail": "Offline fixture data, clearly labelled. No real social posts were retrieved.",
            "classifier": "keyword_match_v1",
        }


class MastodonPublicSignals:
    """Real public posts from Mastodon hashtag timelines (no API key).

    One request per hashtag, throttled and cached by `HttpClient`. A failing
    hashtag is skipped and reported in `errors` rather than failing the set, so
    one dead tag never blanks the panel.
    """

    name = "mastodon"

    def __init__(self, client: HttpClient | None = None, instance: str = MASTODON_INSTANCE,
                 weather_tags: tuple[str, ...] = ("rain", "flood", "weather", "heatwave", "storm"),
                 max_tags: int = 5, cache_ttl_sec: float = 300.0,
                 user_agent: str = "EventFlow-AI/1.0") -> None:
        self.instance = instance.rstrip("/")
        self.weather_tags = tuple(weather_tags)
        self.max_tags = max(1, int(max_tags))
        self.ttl = float(cache_ttl_sec)
        self.client = client or HttpClient(user_agent=user_agent, timeout_sec=6.0, retries=1,
                                          cache_ttl_sec=cache_ttl_sec, cache_size=64)
        self._last: tuple[float, dict] | None = None
        self._lock = threading.Lock()
        self.stats = {"calls": 0, "failures": 0, "cached_answers": 0}

    def _tags(self, topics: list[str]) -> list[str]:
        """Location tags from the active world first, then generic weather tags."""
        seen: list[str] = []
        for candidate in list(topics) + list(self.weather_tags):
            tag = hashtag(candidate)
            if tag and tag not in seen:
                seen.append(tag)
        return seen[: self.max_tags]

    def _fetch_tag(self, tag: str, limit: int) -> list[dict]:
        url = f"{self.instance}/api/v1/timelines/tag/{tag}"
        body = self.client.request_json(self.name, "GET", url, params={"limit": min(40, max(1, limit))})
        if not isinstance(body, list):
            raise ProviderError(self.name, f"#{tag}: response was not a list of statuses")
        return [s for s in body if isinstance(s, dict)]

    def signals(self, topics, *, window_sec=21600, limit=20):
        now = datetime.now(timezone.utc)
        tags = self._tags(list(topics or []))
        location_tags = [t for t in (hashtag(x) for x in (topics or [])) if t]
        if not tags:
            return unavailable("No usable hashtag could be derived for this world.", self.name, [], window_sec)

        cutoff = now - timedelta(seconds=int(window_sec))
        collected: dict[str, dict] = {}
        errors: list[dict[str, str]] = []
        for tag in tags:
            try:
                self.stats["calls"] += 1
                statuses = self._fetch_tag(tag, limit)
            except ProviderError as exc:
                self.stats["failures"] += 1
                errors.append({"tag": f"#{tag}", "reason": exc.reason})
                continue
            except Exception as exc:
                self.stats["failures"] += 1
                errors.append({"tag": f"#{tag}", "reason": f"unexpected error ({type(exc).__name__})"})
                continue
            for status in statuses:
                signal = self._to_signal(status, tag, location_tags, cutoff)
                if signal is not None:
                    collected.setdefault(signal["signal_id"], signal)

        if not collected and errors and len(errors) == len(tags):
            # Every hashtag failed: fall back to the last good set if we have one.
            with self._lock:
                hit = self._last
            if hit is not None:
                age = int(round(time.time() - hit[0]))
                self.stats["cached_answers"] += 1
                return {**hit[1], "availability": "cached", "fetched_at": _iso(now),
                        "detail": f"Every hashtag request failed; showing the set fetched {age}s ago.",
                        "errors": errors, "age_sec": age}
            return {**unavailable("; ".join(f"{e['tag']}: {e['reason']}" for e in errors),
                                  self.name, tags, window_sec), "errors": errors}

        # Local signals first, then by relevance, then newest — a deterministic
        # order, so the same fetch always renders the same way.
        out = sorted(collected.values(),
                     key=lambda s: (s["scope"] != "local", -s["relevance"], s["posted_at"]))[:limit]
        result = {
            "provider": self.name, "availability": "live", "fetched_at": _iso(now),
            "queried_tags": [f"#{t}" for t in tags], "window_sec": int(window_sec),
            "signals": out, "summary": _summarise(out),
            "detail": (None if out else
                       "No public post in the window mentioned weather conditions for these topics."),
            "classifier": "keyword_match_v1",
            **({"errors": errors} if errors else {}),
        }
        with self._lock:
            self._last = (time.time(), dict(result))
        return result

    def _to_signal(self, status: dict, tag: str, location_tags: list[str],
                   cutoff: datetime) -> dict[str, Any] | None:
        posted = _parse_time(status.get("created_at"))
        if posted is None or posted < cutoff:
            return None
        text = strip_html(status.get("content"))
        if not text:
            return None
        lowered = text.lower()
        if not any(anchor in lowered for anchor in WEATHER_ANCHORS):
            return None      # a post from #<city> that is not about the weather
        signal_type, matched = classify(text)
        account = status.get("account") if isinstance(status.get("account"), dict) else {}
        acct = account.get("acct")
        return {
            "signal_id": f"sig_md_{status.get('id')}",
            "source": self.name,
            "source_detail": f"{self.instance.split('//')[-1]} #{tag}",
            "is_real_post": True,
            "posted_at": _iso(posted),
            "author": f"@{acct}" if acct else None,
            "url": status.get("url") if isinstance(status.get("url"), str) else None,
            "text": text,
            "location_topic": f"#{tag}",
            "signal_type": signal_type or "general_weather",
            # The post itself is what a person reported; our labels are derived.
            "observation_kind": "user_report",
            "classification_kind": "derived",
            "matched_terms": matched,
            # "local" = the post named this world's venue/city; "global" = a real
            # weather signal from somewhere else, kept but ranked below local ones.
            "scope": "local" if matches_location(text, tag, location_tags) else "global",
            "relevance": relevance(text, matched, location_tags, tag),
        }


def _summarise(signals: list[dict]) -> dict[str, Any]:
    by_type: dict[str, int] = {}
    for signal in signals:
        by_type[signal["signal_type"]] = by_type.get(signal["signal_type"], 0) + 1
    local = sum(1 for s in signals if s.get("scope") == "local")
    return {
        "count": len(signals),
        "local_count": local,
        "global_count": len(signals) - local,
        "by_type": dict(sorted(by_type.items())),
        "max_relevance": max((s["relevance"] for s in signals), default=None),
    }


_PROVIDER: PublicSignalProvider | None = None


def build_social_provider(cfg: dict | None = None, env: dict | None = None,
                          client: HttpClient | None = None) -> PublicSignalProvider:
    import os

    cfg = dict(cfg or {})
    env = os.environ if env is None else env
    kind = (env.get("EVENTFLOW_SOCIAL_PROVIDER") or cfg.get("provider") or "mastodon").lower()
    if kind in ("none", "off", "disabled"):
        return NoSignals()
    if kind == "fixture":
        return FixtureSignals(cfg.get("fixture_path"))
    if kind != "mastodon":
        log.warning("unknown social provider %r; using mastodon", kind)
    instance = str(cfg.get("instance", MASTODON_INSTANCE))
    if client is None:
        host = instance.split("//")[-1].split("/")[0]
        client = HttpClient(
            user_agent=str(cfg.get("user_agent", "EventFlow-AI/1.0")),
            timeout_sec=float(cfg.get("timeout_sec", 6.0)),
            retries=int(cfg.get("retries", 1)),
            min_interval_sec={host: float(cfg.get("min_interval_sec", 0.5))},
            cache_ttl_sec=float(cfg.get("cache_ttl_sec", 300.0)),
            cache_size=64,
        )
    return MastodonPublicSignals(
        client=client, instance=instance,
        weather_tags=tuple(cfg.get("weather_tags") or ("rain", "flood", "weather", "heatwave", "storm")),
        max_tags=int(cfg.get("max_tags", 5)),
        cache_ttl_sec=float(cfg.get("cache_ttl_sec", 300.0)),
    )


def get_social_provider() -> PublicSignalProvider:
    global _PROVIDER
    if _PROVIDER is None:
        from ..config import get_config

        _PROVIDER = build_social_provider(get_config().raw.get("social") or {})
    return _PROVIDER


def set_social_provider(provider: PublicSignalProvider | None) -> None:
    """Test hook — callers always resolve through `get_social_provider`."""
    global _PROVIDER
    _PROVIDER = provider
