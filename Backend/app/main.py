"""FastAPI application entry point."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

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
    await engine.start()
    log.info("EventFlow AI backend ready on /api/v1")
    try:
        yield
    finally:
        await engine.stop()
        set_engine(None)


app = FastAPI(
    title="EventFlow AI",
    version="1.0.0",
    description="Predictive event-flow orchestration. Contracts: 00_SHARED, 01_BACKEND.",
    lifespan=lifespan,
)

# The frontend runs on Vite's dev server; allow it plus any localhost port.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

install_error_handlers(app)
app.include_router(api_router)
app.include_router(ws_router)


@app.get("/")
async def root() -> dict:
    return {"service": "eventflow-ai", "api": "/api/v1", "docs": "/docs", "ws": "/ws"}
