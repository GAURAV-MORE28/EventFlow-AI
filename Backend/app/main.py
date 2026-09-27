"""FastAPI application entry point."""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api.geo_routes import router as geo_router
from .api.routes import router as api_router
from .api.ws_routes import router as ws_router
from .db.base import create_all
from .db.seed import clear_run_tables, persist_events, seed_topology
from .errors import install_error_handlers
from .services.commander import Commander
from .services.engine import Engine, set_engine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("eventflow")


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_all()
    # Every server start begins the deterministic sim_time sequence over from
    # sim_start (seed 42). Clearing here means a restart against a leftover
    # eventflow.db never collides with the previous run's rows.
    clear_run_tables()
    engine = Engine()
    engine.commander = Commander(engine)
    seed_topology(engine.store)
    persist_events(engine.events.to_generator())
    set_engine(engine)
    engine.prime_state()
    for h in engine.store.history.values():
        h.clear()
    await _restore_active_world(engine)
    await engine.start()
    # Weather runs on its own cadence (minutes), not the 30s cycle: a fetch
    # failure is already handled inside the service and never blocks startup.
    await engine.weather.start()
    log.info("EventFlow AI backend ready on /api/v1")
    try:
        yield
    finally:
        await engine.weather.stop()
        await engine.stop()
        set_engine(None)


async def _restore_active_world(engine: Engine) -> None:
    """Re-activate the last activated generated blueprint (normalised, stored in the
    DB) so a restart does not silently fall back to the demo city. Never fetches
    anything external; on any problem the synthetic demo world stays active."""
    from .config import get_config
    from .geospatial.service import get_blueprint_service

    if not (get_config().raw.get("geospatial") or {}).get("restore_active_world", True):
        return
    svc = get_blueprint_service()
    active = svc.load_active()
    if not active or active.get("source") != "generated_blueprint" or not active.get("blueprint_id"):
        return
    bp = svc.get(active["blueprint_id"])
    if bp is None:
        log.warning("active blueprint %s is no longer stored; staying on the synthetic demo world",
                    active["blueprint_id"])
        return
    try:
        await engine.activate_world(svc.world_from_blueprint(bp, active.get("event")))
        log.info("restored active world %s", active["blueprint_id"])
    except Exception:
        log.exception("could not restore blueprint %s; staying on the synthetic demo world", active["blueprint_id"])


app = FastAPI(
    title="EventFlow AI",
    version="1.0.0",
    description="Predictive event-flow orchestration. Contracts: 00_SHARED, 01_BACKEND.",
    lifespan=lifespan,
)

# The frontend runs on Vite's dev server (any localhost port) plus, in a real
# deployment, whatever origin it's hosted on — EVENTFLOW_ALLOWED_ORIGINS is a
# comma-separated list of exact origins (e.g. "https://eventflow.vercel.app").
_extra_origins = [
    origin.strip()
    for origin in os.environ.get("EVENTFLOW_ALLOWED_ORIGINS", "").split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_origins=_extra_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

install_error_handlers(app)
app.include_router(api_router)
app.include_router(geo_router)
app.include_router(ws_router)


@app.get("/")
async def root() -> dict:
    return {"service": "eventflow-ai", "api": "/api/v1", "docs": "/docs", "ws": "/ws"}
