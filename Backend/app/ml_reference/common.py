"""Shared primitives for the reference ML modules.

Band thresholds live here and are read from config, so 00 §1.3 is encoded once.
"""
from __future__ import annotations

import hashlib
from typing import Any


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def band_from_score(score: float, bands: dict[str, float] | None = None) -> str:
    """00 §1.3 — low 0-30, moderate 31-60, high 61-80, critical 81-100."""
    b = bands or {"low": 30, "moderate": 60, "high": 80}
    if score <= b["low"]:
        return "low"
    if score <= b["moderate"]:
        return "moderate"
    if score <= b["high"]:
        return "high"
    return "critical"


def band_from_utilisation(util: float, critical: float = 0.90, bands: dict[str, float] | None = None) -> str:
    """Utilisation -> band via the same score scale, so the two never disagree."""
    return band_from_score(clamp(util * 100.0, 0.0, 100.0), bands)


def stable_unit(*parts: Any) -> float:
    """A deterministic float in [0,1) from any key. Same inputs -> same value, always.

    Used instead of a live RNG wherever a per-entity constant is needed, so module
    output does not depend on call order (03 §0 rule 5).
    """
    key = "|".join(str(p) for p in parts).encode("utf-8")
    return int(hashlib.sha256(key).hexdigest()[:12], 16) / float(1 << 48)


def variance(values: list[float]) -> float:
    if not values:
        return 0.0
    mean = sum(values) / len(values)
    return sum((v - mean) ** 2 for v in values) / len(values)
