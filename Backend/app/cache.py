"""Cache layer (01_BACKEND_CONTRACT.md §5, "Redis keys").

Redis is used when REDIS_URL is set and the client is installed; otherwise an
in-process dict with the same TTL semantics takes over. Read paths never care
which one they got — that is the whole point of the abstraction.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from .config import redis_url

log = logging.getLogger("eventflow.cache")

# TTLs from 01 §5.
TTL = {
    "state:current": 120,
    "cascade:active": 60,
    "forecast:latest": 60,
    "twin:fidelity": 60,
    "entity": 120,
}


class _MemoryCache:
    backend = "memory"

    def __init__(self) -> None:
        self._data: dict[str, tuple[float, str]] = {}

    def set(self, key: str, value: Any, ttl: int) -> None:
        self._data[key] = (time.monotonic() + ttl, json.dumps(value))

    def get(self, key: str) -> Any | None:
        hit = self._data.get(key)
        if hit is None:
            return None
        expires_at, raw = hit
        if time.monotonic() > expires_at:
            self._data.pop(key, None)
            return None
        return json.loads(raw)

    def clear(self) -> None:
        self._data.clear()


class _RedisCache:
    backend = "redis"

    def __init__(self, client: Any) -> None:
        self._client = client

    def set(self, key: str, value: Any, ttl: int) -> None:
        try:
            self._client.setex(key, ttl, json.dumps(value))
        except Exception:  # a cache failure must never break a request
            log.warning("redis set failed for %s; continuing", key, exc_info=False)

    def get(self, key: str) -> Any | None:
        try:
            raw = self._client.get(key)
        except Exception:
            log.warning("redis get failed for %s; continuing", key, exc_info=False)
            return None
        return json.loads(raw) if raw else None

    def clear(self) -> None:
        try:
            self._client.flushdb()
        except Exception:
            pass


def build_cache() -> _MemoryCache | _RedisCache:
    url = redis_url()
    if url:
        try:
            import redis  # type: ignore

            client = redis.Redis.from_url(url, decode_responses=True)
            client.ping()
            log.info("cache backend: redis (%s)", url)
            return _RedisCache(client)
        except Exception as exc:
            log.warning("redis unavailable (%s); falling back to in-process cache", exc)
    log.info("cache backend: in-process memory")
    return _MemoryCache()


CACHE = build_cache()
