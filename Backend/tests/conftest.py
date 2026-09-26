"""Test-session setup.

The suite runs the real FastAPI app, which persists to SQLite. Point it at a
throwaway database *before* `app` is imported, so running the tests never
touches (or clears the run tables of) a developer's `Backend/eventflow.db`.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

if "DATABASE_URL" not in os.environ:
    _tmp = Path(tempfile.mkdtemp(prefix="eventflow-tests-")) / "test.db"
    os.environ["DATABASE_URL"] = f"sqlite:///{_tmp.as_posix()}"
