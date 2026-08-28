"""Database engine, session management, and schema creation."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event, inspect
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


def _build_engine():
    url = settings.database_url
    connect_args: dict = {}
    if url.startswith("sqlite"):
        # The event-ingest consumer writes from a background task while
        # request handlers read, so the connection must cross threads.
        connect_args["check_same_thread"] = False
        db_path = settings.resolve(url.split("sqlite:///")[-1])
        db_path.parent.mkdir(parents=True, exist_ok=True)
        url = f"sqlite:///{db_path}"
    return create_engine(url, connect_args=connect_args, pool_pre_ping=True, future=True)


engine = _build_engine()


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_conn, _record):
    """WAL keeps the event writer from blocking page reads."""
    if not settings.database_url.startswith("sqlite"):
        return
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA busy_timeout=5000")
    cur.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding a request-scoped session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Standalone session for background tasks and scripts."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_db() -> None:
    from app import models  # noqa: F401  (registers mappers)

    Base.metadata.create_all(bind=engine)
    with engine.begin() as connection:
        added = _apply_additive_columns(connection)
    if added:
        logger.info("Schema patched, columns added: %s", ", ".join(added))


# --------------------------------------------------------------------------
# Additive schema patches
#
# `create_all` creates missing *tables*. It will not add a column to a table
# that already exists, so a database created before a column was introduced goes
# on working right up until the first query names that column and fails with
# "no such column" - which in practice means after a pull, on someone else's
# machine, on a page that used to work.
#
# There is no Alembic here on purpose: the schema is eight tables and every
# change so far has been additive with a usable default. That holds only while
# these stay additive - a column that needs backfilling, a rename, or a type
# change is the point where this stops being adequate and a real migration tool
# earns its place.
# --------------------------------------------------------------------------
ADDITIVE_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("products", "image_url", "TEXT DEFAULT ''"),
    ("products", "image_credit", "VARCHAR(160) DEFAULT ''"),
    ("products", "image_credit_url", "TEXT DEFAULT ''"),
)


def _apply_additive_columns(connection) -> list[str]:
    """Add any column in ADDITIVE_COLUMNS the database is missing.

    Returns the columns actually added, which is what makes this testable: a
    second call against the same database must report nothing. Tables absent
    entirely are skipped rather than patched, because `create_all` has just
    built those complete from the models.
    """
    inspector = inspect(connection)
    tables = set(inspector.get_table_names())
    added: list[str] = []

    for table, column, ddl in ADDITIVE_COLUMNS:
        if table not in tables:
            continue
        if column in {c["name"] for c in inspector.get_columns(table)}:
            continue
        # Identifiers here are literals in the tuple above, never user input.
        connection.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        added.append(f"{table}.{column}")

    return added
