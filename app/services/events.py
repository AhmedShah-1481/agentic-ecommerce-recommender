"""Event ingestion.

The POST handler must return before any database work happens, so events go
onto a bounded asyncio.Queue and a single background consumer drains them in
batches. That gives us:

  * a fast 202 on the request path (no DB latency in front of the user)
  * one multi-row INSERT per batch instead of one INSERT per event
  * back-pressure handling: if the queue is full we drop the OLDEST events,
    because recent behaviour is what drives recommendations

Duplicate delivery is expected (the client retries failed flushes), so writes
are idempotent on the client-generated `event_id`.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import insert, select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import Event, UserProfileState

logger = logging.getLogger(__name__)

QUEUE_MAX = 5000
DRAIN_BATCH = 200
DRAIN_INTERVAL = 0.5

# Events that indicate genuine interest. Used to decide when the agent is
# worth running - see services/triggers.py.
MEANINGFUL_TYPES = {"product_view", "search", "product_click", "add_to_cart", "dwell"}

_queue: asyncio.Queue | None = None
_consumer_task: asyncio.Task | None = None
_stats = {"received": 0, "written": 0, "duplicates": 0, "dropped": 0, "batches": 0}


def get_queue() -> asyncio.Queue:
    global _queue
    if _queue is None:
        _queue = asyncio.Queue(maxsize=QUEUE_MAX)
    return _queue


def stats() -> dict:
    q = _queue
    return {**_stats, "queue_depth": q.qsize() if q else 0, "queue_max": QUEUE_MAX}


def enqueue(events: list[dict]) -> int:
    """Non-blocking hand-off from the request handler. Returns count accepted."""
    queue = get_queue()
    accepted = 0
    for event in events:
        try:
            queue.put_nowait(event)
            accepted += 1
        except asyncio.QueueFull:
            # Shed the oldest event to make room for this newer one.
            try:
                queue.get_nowait()
                queue.task_done()
                _stats["dropped"] += 1
                queue.put_nowait(event)
                accepted += 1
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                _stats["dropped"] += 1
    _stats["received"] += accepted
    return accepted


def _parse_ts(value) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        return datetime.fromisoformat(text).astimezone(timezone.utc).replace(tzinfo=None)
    except (ValueError, TypeError):
        return None


def _write_batch(db: Session, batch: list[dict]) -> int:
    """One INSERT for the whole batch, skipping ids we already hold."""
    if not batch:
        return 0

    # Collapse duplicates inside the batch itself first.
    by_id: dict[str, dict] = {}
    for event in batch:
        eid = event.get("event_id")
        if eid:
            by_id[eid] = event

    if not by_id:
        return 0

    existing = set(
        db.scalars(select(Event.event_id).where(Event.event_id.in_(list(by_id))))
    )
    fresh = [e for eid, e in by_id.items() if eid not in existing]
    _stats["duplicates"] += len(by_id) - len(fresh)

    if not fresh:
        return 0

    rows = []
    for event in fresh:
        rows.append(
            {
                "event_id": event["event_id"],
                "user_id": event.get("user_id"),
                "session_id": (event.get("session_id") or "")[:64],
                "event_type": (event.get("event_type") or "unknown")[:40],
                "product_id": event.get("product_id"),
                "search_query": (event.get("search_query") or "")[:255],
                "path": (event.get("path") or "")[:255],
                "dwell_ms": int(event.get("dwell_ms") or 0),
                "scroll_pct": int(event.get("scroll_pct") or 0),
                "meta": event.get("meta") or {},
                "client_ts": _parse_ts(event.get("client_ts")),
                "server_ts": datetime.now(timezone.utc).replace(tzinfo=None),
            }
        )

    stmt = insert(Event)
    # Belt-and-braces against a race between the SELECT above and this INSERT
    # (two workers draining the same duplicated event_id).
    dialect = db.bind.dialect.name if db.bind is not None else ""
    if dialect == "sqlite":
        stmt = stmt.prefix_with("OR IGNORE")
    elif dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        stmt = pg_insert(Event).on_conflict_do_nothing(index_elements=["event_id"])

    db.execute(stmt, rows)

    # Bump the per-user counters that gate recommendation regeneration.
    counts: dict[int, int] = {}
    for event in fresh:
        uid = event.get("user_id")
        if uid and event.get("event_type") in MEANINGFUL_TYPES:
            counts[uid] = counts.get(uid, 0) + 1

    for uid, count in counts.items():
        state = db.get(UserProfileState, uid)
        if state is None:
            state = UserProfileState(user_id=uid, events_since_last_rec=0)
            db.add(state)
        state.events_since_last_rec = (state.events_since_last_rec or 0) + count

    db.commit()
    return len(rows)


async def _drain_once() -> int:
    """Pull everything currently queued (up to DRAIN_BATCH) and write it."""
    queue = get_queue()
    batch: list[dict] = []

    # Only the timeout is caught below: an idle queue is not an error. A
    # CancelledError must be allowed through, or the consumer task becomes
    # un-cancellable - `_consumer` loops straight into another drain, its own
    # `except CancelledError: break` never fires, and `stop_consumer`'s
    # `await _consumer_task` hangs the whole shutdown with nothing logged.
    # Nothing is lost by letting it through: stop_consumer drains the queue
    # before it cancels. Pinned by tests/test_event_consumer_shutdown.py.
    try:
        first = await asyncio.wait_for(queue.get(), timeout=DRAIN_INTERVAL)
        batch.append(first)
        queue.task_done()
    except asyncio.TimeoutError:
        return 0

    while len(batch) < DRAIN_BATCH:
        try:
            batch.append(queue.get_nowait())
            queue.task_done()
        except asyncio.QueueEmpty:
            break

    def _work() -> int:
        db = SessionLocal()
        try:
            return _write_batch(db, batch)
        finally:
            db.close()

    try:
        # Keep the sync DB write off the event loop.
        written = await asyncio.to_thread(_work)
        _stats["written"] += written
        _stats["batches"] += 1
        return written
    except Exception:
        logger.exception("Event batch write failed (%d events lost)", len(batch))
        return 0


async def _consumer() -> None:
    logger.info("Event consumer started")
    while True:
        try:
            await _drain_once()
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Event consumer error; continuing")
            await asyncio.sleep(1)
    logger.info("Event consumer stopped")


async def start_consumer() -> None:
    global _consumer_task
    if _consumer_task is None or _consumer_task.done():
        _consumer_task = asyncio.create_task(_consumer())


async def stop_consumer() -> None:
    """Flush what's queued, then stop. Called from the lifespan shutdown."""
    global _consumer_task
    if _consumer_task is None:
        return

    queue = get_queue()
    deadline = 3.0
    while not queue.empty() and deadline > 0:
        await _drain_once()
        deadline -= 0.2

    _consumer_task.cancel()
    try:
        await _consumer_task
    except asyncio.CancelledError:
        pass
    _consumer_task = None
