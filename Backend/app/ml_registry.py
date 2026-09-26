"""The ONLY place ML is instantiated (01_BACKEND_CONTRACT.md §6).

Resolution order per module:
  1. the real ML workstream package — `ml.forecaster.Forecaster` etc.
  2. `app.ml_reference` — the deterministic stand-in in this repo

So the day `ML/forecaster.py` lands, the backend picks it up with no code change
and `/health` starts reporting the new `active_source`. Nothing else in the
backend imports an ML class directly.

Every call goes through `call_ml()`, which enforces the §2 latency budget and
routes to the module's own `.fallback()` on timeout or exception. A fallback is
a normal operating state, not an error — the backend never 500s because a model
was unavailable.
"""
from __future__ import annotations

import asyncio
import importlib
import logging
import sys
import time
from pathlib import Path
from typing import Any, Callable

from . import ml_reference
from .config import REPO_ROOT, get_config

log = logging.getLogger("eventflow.ml")

# Module name in `ml/` -> class name. Matches 03_ML_CONTRACT.md §1 exactly.
ML_MODULES: dict[str, tuple[str, str]] = {
    "forecaster": ("forecaster", "Forecaster"),
    "cascade": ("cascade", "CascadePredictor"),
    "twin": ("twin", "AssimilatedTwin"),
    "equilibrium": ("equilibrium", "EquilibriumSolver"),
    "optimiser": ("optimiser", "InterventionOptimiser"),
    "risk": ("risk", "RiskScorer"),
    "anomaly": ("anomaly", "AnomalyDetector"),
    "generator": ("generator", "SyntheticGenerator"),
}


def _ml_package_names() -> list[str]:
    """`ml` is the contract name; `ML` is the folder this repo actually has."""
    return ["ml", "ML"]


def _ensure_repo_on_path() -> None:
    root = str(REPO_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)


def _load_class(module_key: str) -> tuple[type, str]:
    """Return (class, provenance) — provenance is 'ml' or 'reference'."""
    _ensure_repo_on_path()
    module_name, class_name = ML_MODULES[module_key]

    for pkg in _ml_package_names():
        candidate = REPO_ROOT / pkg / f"{module_name}.py"
        if not candidate.exists():
            continue
        try:
            module = importlib.import_module(f"{pkg}.{module_name}")
            cls = getattr(module, class_name)
            log.info("ml.%s -> %s.%s (ML workstream)", module_key, pkg, module_name)
            return cls, "ml"
        except Exception as exc:
            log.warning(
                "ml.%s found at %s but failed to import as %r (%s); trying next candidate",
                module_key, candidate, pkg, exc,
            )
            continue  # `ml`/`ML` alias the same path on a case-insensitive filesystem (Windows);
            # a failed import under one case must not stop the other from being tried.

    log.warning("ml.%s not found under ml/ or ML/; using reference implementation", module_key)
    return getattr(ml_reference, class_name), "reference"


class MLRegistry:
    def __init__(self) -> None:
        cfg = get_config()
        raw = cfg.raw
        seed = cfg.demo_seed
        thresholds = raw["thresholds"]

        from .topology import build_topology

        critical_by_entity = {
            n["entity_id"]: cfg.thresholds_for(n["entity_type"])[1] for n in build_topology()["nodes"]
        }

        # Each module gets its own config block plus the shared thresholds it needs.
        # ML modules receive plain dicts only — never a Config object, never a session.
        def block(name: str, **extra: Any) -> dict:
            out = dict(raw.get(name, {}))
            out["seed"] = seed
            out["critical_utilisation"] = thresholds["critical_utilisation"]
            out["warning_utilisation"] = thresholds.get("warning_utilisation", 0.75)
            out["risk_bands"] = thresholds["risk_bands"]
            out["thresholds_by_type"] = thresholds.get("by_type", {})
            out["critical_by_entity"] = critical_by_entity
            out.update(extra)
            return out

        self.provenance: dict[str, str] = {}
        self._classes: dict[str, type] = {}
        for key in ML_MODULES:
            cls, prov = _load_class(key)
            self._classes[key] = cls
            self.provenance[key] = prov

        self.forecaster = self._classes["forecaster"](block("forecaster", step_sec=cfg.cycle_sec))
        self.cascade = self._classes["cascade"](block("cascade"))
        self.twin = self._classes["twin"](block("twin"))
        self.equilibrium = self._classes["equilibrium"](block("equilibrium"))
        self.optimiser = self._classes["optimiser"](block("optimiser"))
        self.risk = self._classes["risk"](block("risk"))
        self.anomaly = self._classes["anomaly"](block("anomaly"))
        self._generator_cls = self._classes["generator"]
        self._generator_config = block(
            "generator",
            cycle_sec=cfg.cycle_sec,
            demand=raw.get("demand", {}),
            hospitality=raw.get("hospitality", {}),
            sim_start_time=raw["event"]["sim_start_time"],
        )

    def build_generator(self, topology: dict, seed: int) -> Any:
        """Build the city model (`city_model` in config.yaml).

        `flow` is the production SyntheticGenerator; an `ML/generator.py`
        drop-in overrides it through the normal resolution order above.
        """
        choice = str(get_config().raw.get("city_model", "flow"))
        if choice != "flow":
            raise ValueError(f"unknown city_model '{choice}' (supported: flow)")
        return self._generator_cls(self._generator_config, topology, seed)

    def as_dict(self) -> dict[str, Any]:
        return {
            "forecaster": self.forecaster,
            "cascade": self.cascade,
            "twin": self.twin,
            "equilibrium": self.equilibrium,
            "optimiser": self.optimiser,
            "risk": self.risk,
            "anomaly": self.anomaly,
        }


# --- 01 §6 rules 1 & 2: timeout-wrapped, fallback on failure ------------------
async def call_ml(
    name: str,
    primary: Callable[..., Any],
    fallback: Callable[..., Any] | None,
    budget_sec: float,
    *args: Any,
    **kwargs: Any,
) -> tuple[Any, bool, float]:
    """Run an ML call under its budget.

    Returns (result, degraded, elapsed_ms). `degraded` is True when the fallback
    produced the value — the caller uses it to set the `source` field honestly.
    """
    started = time.perf_counter()
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(primary, *args, **kwargs), timeout=budget_sec
        )
        return result, False, (time.perf_counter() - started) * 1000.0
    except asyncio.TimeoutError:
        log.warning("ml.%s exceeded its %.0fms budget; falling back", name, budget_sec * 1000)
    except Exception:
        log.exception("ml.%s raised; falling back", name)

    if fallback is None:
        return None, True, (time.perf_counter() - started) * 1000.0
    try:
        result = fallback(*args, **kwargs)
    except Exception:
        log.exception("ml.%s fallback also failed", name)
        result = None
    return result, True, (time.perf_counter() - started) * 1000.0
