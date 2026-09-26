"""Engine and session factory.

Deviation from 01 §5, stated openly: the DDL specifies PostgreSQL 16 + PostGIS.
The default here is SQLite so the stack runs with zero infrastructure, and
`DATABASE_URL=postgresql+psycopg://...` switches to Postgres with no code change.
The only schema consequence is `geom GEOGRAPHY(POINT, 4326)` being stored as the
`lat`/`lon` doubles the API exposes anyway.
"""
from __future__ import annotations

import logging

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from ..config import database_url

log = logging.getLogger("eventflow.db")


class Base(DeclarativeBase):
    pass


_url = database_url()
_kwargs: dict = {"future": True, "pool_pre_ping": True}
if _url.startswith("sqlite"):
    _kwargs["connect_args"] = {"check_same_thread": False}
    _kwargs.pop("pool_pre_ping")

engine = create_engine(_url, **_kwargs)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def create_all() -> None:
    from . import models  # noqa: F401  (import registers the mappers)

    Base.metadata.create_all(engine)
    _add_missing_columns()
    log.info("schema ready on %s", engine.url.render_as_string(hide_password=True))


# Columns added to existing tables after first release (additive contract
# changes). `create_all` never alters an existing table, so a database created
# by an earlier version gets them here.
ADDED_COLUMNS = {
    "cascade_prediction": {"model_version": "TEXT"},
}


def _add_missing_columns() -> None:
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    for table, columns in ADDED_COLUMNS.items():
        if not inspector.has_table(table):
            continue
        present = {c["name"] for c in inspector.get_columns(table)}
        for name, ddl_type in columns.items():
            if name not in present:
                with engine.begin() as conn:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl_type}"))
                log.info("added column %s.%s", table, name)
