"""Runs the agent and persists its output.

Reading a recommendation is always a plain indexed SELECT. Generating one
happens in the background behind the trigger gates in services/triggers.py,
so a page render never waits on the model (stale-while-revalidate).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.graph import recommendation_graph
from app.config import settings
from app.db import SessionLocal
from app.models import Product, Recommendation, RecommendationItem, User
from app.services import profile as profile_service
from app.services import triggers

logger = logging.getLogger(__name__)

# Guards against two coroutines in THIS process racing for the same user.
# The DB lease in triggers.py covers multi-process deployments.
_locks: dict[int, asyncio.Lock] = {}


def _lock_for(user_id: int) -> asyncio.Lock:
    lock = _locks.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        _locks[user_id] = lock
    return lock


def get_current(db: Session, user_id: int) -> Recommendation | None:
    """The cached recommendation. This is what page renders read."""
    return triggers.current_recommendation(db, user_id)


async def maybe_generate(user_id: int, *, force: bool = False, mode: str = "dashboard") -> str:
    """Evaluate the gates and generate only if warranted.

    Returns the decision reason - surfaced in the admin view so the triggering
    logic is inspectable rather than opaque.
    """
    lock = _lock_for(user_id)
    if lock.locked() and not force:
        return "in_flight_local"

    async with lock:
        db = SessionLocal()
        try:
            snapshot = profile_service.build_snapshot(db, user_id)
            decision = triggers.evaluate(db, user_id, snapshot, force=force)
            if not decision.should_generate:
                return decision.reason

            if not triggers.acquire_lease(db, user_id):
                return "lease_taken"
        finally:
            db.close()

        try:
            await _generate(user_id, mode=mode, trigger_reason=decision.reason)
            return f"generated:{decision.reason}"
        except Exception:
            logger.exception("Recommendation generation failed for user %s", user_id)
            db = SessionLocal()
            try:
                triggers.release_lease(db, user_id)
            finally:
                db.close()
            return "error"


async def _generate(user_id: int, *, mode: str, trigger_reason: str) -> Recommendation | None:
    db = SessionLocal()
    try:
        user = db.get(User, user_id)
        if user is None:
            return None

        snapshot = profile_service.build_snapshot(db, user_id)
        initial = {
            "user_id": user_id,
            "user_name": user.name or "",
            "profile": snapshot.to_prompt_dict(),
            "seen_product_ids": snapshot.viewed_product_ids,
            "preferred_brand": snapshot.preferred_brand,
            "mode": mode,
            "refine_attempts": 0,
            "trace": [],
        }
    finally:
        db.close()

    result = await recommendation_graph.ainvoke(initial)

    db = SessionLocal()
    try:
        return _persist(db, user_id, snapshot, result, trigger_reason)
    finally:
        db.close()


def _persist(
    db: Session,
    user_id: int,
    snapshot: profile_service.ProfileSnapshot,
    result: dict,
    trigger_reason: str,
) -> Recommendation | None:
    selected = result.get("selected") or []
    if not selected:
        triggers.release_lease(db, user_id)
        return None

    # Only real catalog rows survive to storage.
    valid_ids = set(
        db.scalars(select(Product.id).where(Product.id.in_([s["id"] for s in selected])))
    )
    selected = [s for s in selected if s["id"] in valid_ids]
    if not selected:
        triggers.release_lease(db, user_id)
        return None

    now = datetime.now(timezone.utc).replace(tzinfo=None)

    for previous in db.scalars(
        select(Recommendation).where(
            Recommendation.user_id == user_id, Recommendation.is_current.is_(True)
        )
    ):
        previous.is_current = False

    trace = result.get("trace") or []
    rec = Recommendation(
        user_id=user_id,
        headline=result.get("headline") or "Picked for you",
        narrative=result.get("narrative") or "",
        trigger_reason=trigger_reason[:120],
        behavior_signature=snapshot.signature,
        detected_interests=result.get("interests") or [],
        model=settings.gemini_chat_model if result.get("used_llm") else "none",
        is_current=True,
        is_fallback=bool(result.get("is_fallback")),
        generated_at=now,
        expires_at=now + timedelta(hours=settings.rec_ttl_hours),
        agent_trace={
            "nodes": trace,
            "inferred_intent": result.get("inferred_intent", ""),
            "unmet_needs": result.get("unmet_needs", []),
            "queries": result.get("queries", []),
            "refine_attempts": result.get("refine_attempts", 0),
            "retrieval_quality": round(result.get("retrieval_quality", 0.0), 3),
            "path": [entry.get("node") for entry in trace],
        },
    )
    db.add(rec)
    db.flush()

    reasons = result.get("item_reasons") or {}
    for rank, item in enumerate(selected, start=1):
        db.add(
            RecommendationItem(
                rec_id=rec.id,
                product_id=item["id"],
                rank=rank,
                reason=reasons.get(item["id"], ""),
                retrieval_score=float(item.get("score", 0.0)),
                final_score=float(item.get("final_score", 0.0)),
            )
        )

    db.commit()
    triggers.mark_generated(db, user_id, snapshot.signature, snapshot.top_categories)
    logger.info(
        "Generated recommendation %s for user %s (%s, %d items)",
        rec.id, user_id, trigger_reason, len(selected),
    )
    return rec


# asyncio only holds a weak reference to a running task, so a task nobody keeps
# can be garbage-collected mid-await. Without this set, a refresh could vanish
# part-way through - intermittently, under load, and only in production.
_background: set[asyncio.Task] = set()


def schedule_refresh(user_id: int, *, mode: str = "dashboard") -> None:
    """Fire-and-forget refresh. Never awaited by a request handler."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(_safe_refresh(user_id, mode))
    _background.add(task)
    task.add_done_callback(_background.discard)


async def _safe_refresh(user_id: int, mode: str) -> None:
    try:
        reason = await maybe_generate(user_id, mode=mode)
        logger.debug("Background refresh for user %s: %s", user_id, reason)
    except Exception:
        logger.exception("Background refresh failed for user %s", user_id)
