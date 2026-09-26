"""Small HTTP client shared by the geospatial providers.

Every external request goes through here so the rules are enforced in one place:
a descriptive User-Agent (OSM services require one), a timeout, bounded retries,
a minimum interval between requests to the same host (Nominatim allows at most
1 request/second), and a result cache. The transport is injectable so tests never
touch the network.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

log = logging.getLogger("eventflow.geospatial.http")

# transport(method, url, headers, body_bytes, timeout) -> (status, body_bytes)
Transport = Callable[[str, str, dict[str, str], bytes | None, float], tuple[int, bytes]]


class ProviderError(Exception):
    """An external provider could not answer (network, HTTP status, rate limit, bad payload)."""

    def __init__(self, provider: str, reason: str, status: int | None = None) -> None:
        super().__init__(f"{provider}: {reason}")
        self.provider = provider
        self.reason = reason
        self.status = status


def urllib_transport(method: str, url: str, headers: dict[str, str], body: bytes | None,
                     timeout: float) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (trusted configured hosts)
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read() if exc.fp else b""


class HttpClient:
    def __init__(self, user_agent: str, timeout_sec: float = 30.0, retries: int = 1,
                 min_interval_sec: dict[str, float] | None = None, cache_ttl_sec: float = 3600.0,
                 cache_size: int = 256, transport: Transport | None = None) -> None:
        self.user_agent = user_agent
        self.timeout = float(timeout_sec)
        self.retries = max(0, int(retries))
        self.min_interval = dict(min_interval_sec or {})
        self.ttl = float(cache_ttl_sec)
        self.size = int(cache_size)
        self.transport = transport or urllib_transport
        self._last: dict[str, float] = {}
        self._cache: dict[tuple, tuple[float, Any]] = {}
        self._lock = threading.Lock()
        self.stats = {"requests": 0, "cache_hits": 0, "failures": 0}

    def _throttle(self, host: str) -> None:
        gap = self.min_interval.get(host)
        if not gap:
            return
        with self._lock:
            now = time.monotonic()
            wait = self._last.get(host, 0.0) + gap - now
            self._last[host] = max(now, self._last.get(host, 0.0) + gap)
        if wait > 0:
            time.sleep(wait)

    def request_json(self, provider: str, method: str, url: str, *, params: dict | None = None,
                     body: bytes | None = None, headers: dict[str, str] | None = None,
                     cache: bool = True, timeout: float | None = None) -> Any:
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        key = (method, url, body)
        now = time.monotonic()
        if cache:
            with self._lock:
                hit = self._cache.get(key)
                if hit and now - hit[0] < self.ttl:
                    self.stats["cache_hits"] += 1
                    return hit[1]
        hdrs = {"User-Agent": self.user_agent, "Accept": "application/json", **(headers or {})}
        host = urllib.parse.urlparse(url).netloc
        last_error: ProviderError | None = None
        for attempt in range(self.retries + 1):
            self._throttle(host)
            self.stats["requests"] += 1
            try:
                status, raw = self.transport(method, url, hdrs, body, timeout or self.timeout)
            except Exception as exc:  # timeout, DNS, connection reset
                last_error = ProviderError(provider, f"network error ({type(exc).__name__})")
            else:
                if status == 429:
                    last_error = ProviderError(provider, "rate limited by the provider", 429)
                elif status >= 400:
                    last_error = ProviderError(provider, f"HTTP {status}", status)
                else:
                    try:
                        data = json.loads(raw.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        last_error = ProviderError(provider, "response was not valid JSON", status)
                    else:
                        if cache:
                            with self._lock:
                                if len(self._cache) >= self.size:
                                    self._cache.pop(next(iter(self._cache)))
                                self._cache[key] = (now, data)
                        return data
            self.stats["failures"] += 1
            log.warning("%s request failed (attempt %d/%d): %s", provider, attempt + 1, self.retries + 1,
                        last_error.reason if last_error else "?")
            if attempt < self.retries:
                time.sleep(min(0.5 * (2 ** attempt), 2.0))
        raise last_error or ProviderError(provider, "unknown failure")
