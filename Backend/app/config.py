"""Configuration loading. `config.yaml` (01 §7) is authoritative; env vars override infra only."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

BACKEND_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = BACKEND_ROOT.parent


class Config:
    def __init__(self, raw: dict[str, Any]) -> None:
        self._raw = raw

    def __getitem__(self, key: str) -> Any:
        return self._raw[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self._raw.get(key, default)

    @property
    def raw(self) -> dict[str, Any]:
        return self._raw

    # --- frequently used, typed accessors -------------------------------
    @property
    def cycle_sec(self) -> int:
        return int(self._raw["cycle_sec"])

    @property
    def demo_seed(self) -> int:
        return int(self._raw["demo_seed"])

    @property
    def critical_utilisation(self) -> float:
        return float(self._raw["thresholds"]["critical_utilisation"])

    @property
    def horizons_sec(self) -> list[int]:
        return list(self._raw["forecaster"]["horizons_sec"])

    def thresholds_for(self, entity_type: str | None) -> tuple[float, float]:
        """(warning, critical) utilisation for an entity type."""
        t = self._raw["thresholds"]
        by = (t.get("by_type") or {}).get(entity_type or "", {})
        return (float(by.get("warning", t.get("warning_utilisation", 0.75))),
                float(by.get("critical", t["critical_utilisation"])))

    def budget_ms(self, step: str) -> int:
        return int(self._raw["budgets_ms"][step])

    def budget_sec(self, step: str) -> float:
        return self.budget_ms(step) / 1000.0


@lru_cache(maxsize=1)
def get_config() -> Config:
    path = Path(os.getenv("EVENTFLOW_CONFIG", BACKEND_ROOT / "config.yaml"))
    if not path.is_absolute():
        path = BACKEND_ROOT / path
    with open(path, "r", encoding="utf-8") as fh:
        return Config(yaml.safe_load(fh))


def database_url() -> str:
    return os.getenv("DATABASE_URL", f"sqlite:///{BACKEND_ROOT / 'eventflow.db'}")


def redis_url() -> str | None:
    return os.getenv("REDIS_URL") or None


def local_llm() -> tuple[str, str] | None:
    """Optional free/local LLM (e.g. Ollama). Off unless LOCAL_LLM_URL is set.

    The Commander is fully functional without it — see services/commander.py.
    """
    url = os.getenv("LOCAL_LLM_URL")
    if not url:
        return None
    return url, os.getenv("LOCAL_LLM_MODEL", "llama3.2")
