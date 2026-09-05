"""Time helpers.

00 §0: every event-domain timestamp is ISO 8601 UTC with a `Z` suffix, and it is
*simulated* time. Wall clock appears only as `server_time`.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


def iso(dt: datetime) -> str:
    """Format as `2026-09-04T14:32:00Z` — always UTC, always `Z`, no microseconds."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def server_now() -> str:
    return iso(datetime.now(timezone.utc))


def shift(value: str, seconds: float) -> str:
    return iso(parse(value) + timedelta(seconds=seconds))
