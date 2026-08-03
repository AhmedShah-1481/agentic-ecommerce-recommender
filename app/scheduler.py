"""Background scheduler: proactive digests plus the housekeeping jobs.

Three jobs, started from the FastAPI lifespan:

  * daily digest  - for each user with activity that day, run the agent in
                    digest mode and deliver a real email. This is the
                    "proactive delivery" bonus: nobody clicks a button.
  * reconcile     - every few minutes, repair any SQL/vector drift.
  * housekeeping  - hourly: expire past-TTL recommendations and trim old events.

Every job body is wrapped so a failure is logged and the scheduler survives it.
APScheduler's `AsyncIOScheduler` shares the app's event loop, which is what lets
the digest job await the agent directly.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.config import settings
from app.db import SessionLocal, session_scope
from app.models import (
    DigestLog,
    Event,
    Recommendation,
    RecommendationItem,
    User,
)
from app.services import mailer
from app.services import recommendations as rec_service
from app.services import triggers
from app.templating import templates

logger = logging.getLogger(__name__)

# A user needs at least this many events today before a digest is worth sending.
# Proactive email about nothing is spam, and it would also mean an LLM call for
# a profile with no signal in it.
DIGEST_MIN_EVENTS = 3


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# --------------------------------------------------------------------------
# Digest
# --------------------------------------------------------------------------
def _digest_candidates(db: Session, since: datetime) -> list[int]:
    """Users with enough activity since `since` to have something to say."""
    rows = db.execute(
        select(Event.user_id, func.count(Event.id))
        .where(Event.user_id.isnot(None), Event.server_ts >= since)
        .group_by(Event.user_id)
        .having(func.count(Event.id) >= DIGEST_MIN_EVENTS)
    ).all()
    return [row[0] for row in rows]


def _claim_digest(db: Session, user_id: int, day: date) -> bool:
    """Insert the digest_log row up front and treat a conflict as 'already sent'.

    Claiming before sending (rather than recording after) is what makes a double
    scheduler run - two workers, or a restart at 16:00 - a no-op instead of two
    emails. The unique (user_id, digest_date) index does the arbitration.
    """
    row = DigestLog(user_id=user_id, digest_date=day, status="pending")
    db.add(row)
    try:
        db.commit()
        return True
    except IntegrityError:
        db.rollback()
        return False


def _finish_digest(
    db: Session, user_id: int, day: date, *, status: str, detail: str, rec_id: int | None
) -> None:
    row = db.scalars(
        select(DigestLog).where(DigestLog.user_id == user_id, DigestLog.digest_date == day)
    ).first()
    if row is not None:
        row.status = status
        row.detail = detail[:500]
        row.rec_id = rec_id
        db.commit()


def render_digest(user: User, rec: Recommendation) -> str:
    """Render the digest email. Separate from delivery so it is testable."""
    items = [
        {
            "product_id": item.product_id,
            "title": item.product.title if item.product else "",
            "category": item.product.category if item.product else "",
            "level": item.product.level if item.product else "",
            "price_label": ("Free" if not item.product or not item.product.price
                            else f"${item.product.price:,.0f}"),
            "reason": item.reason,
        }
        for item in rec.items
        if item.product is not None
    ]

    template = templates.env.get_template("email/digest.html")
    return template.render(
        app_name=settings.app_name,
        base_url=settings.base_url.rstrip("/"),
        user=user,
        headline=rec.headline or "Picked for you",
        narrative=rec.narrative,
        items=items,
        detected_interests=(rec.detected_interests or [])[:4],
        digest_date=rec.generated_at.strftime("%d %b"),
    )


async def _digest_for_user(user_id: int, day: date) -> str:
    """Generate (if warranted) and deliver one user's digest."""
    db = SessionLocal()
    try:
        user = db.get(User, user_id)
        if user is None or not user.email:
            return "no_user"
        if not _claim_digest(db, user_id, day):
            return "already_sent"
    finally:
        db.close()

    # The digest goes through the same gates as interactive traffic, deliberately:
    # if today's behaviour produced the same signature, the cached recommendation
    # is the correct answer and re-generating it would buy a second LLM call and
    # identical copy. Forcing is reserved for the one case where there is nothing
    # to send at all.
    with session_scope() as db:
        has_rec = triggers.current_recommendation(db, user_id) is not None

    decision = await rec_service.maybe_generate(
        user_id, mode="digest", force=not has_rec
    )

    db = SessionLocal()
    try:
        user = db.get(User, user_id)
        rec = db.scalars(
            select(Recommendation)
            .where(Recommendation.user_id == user_id, Recommendation.is_current.is_(True))
            .options(selectinload(Recommendation.items).selectinload(RecommendationItem.product))
            .order_by(Recommendation.generated_at.desc())
            .limit(1)
        ).first()

        if rec is None or not rec.items:
            _finish_digest(db, user_id, day, status="skipped",
                           detail=f"no recommendation ({decision})", rec_id=None)
            return "no_recommendation"

        html = render_digest(user, rec)
        subject = rec.headline or f"{settings.app_name}: your next courses"
        ok, detail = await mailer.send(user.email, subject, html)

        _finish_digest(
            db, user_id, day,
            status="sent" if ok else "failed",
            detail=detail,
            rec_id=rec.id,
        )
        return "sent" if ok else "failed"
    finally:
        db.close()


async def run_digests(*, day: date | None = None, window_hours: int = 24) -> dict:
    """The daily job. Also callable by hand, which is how it gets demoed."""
    day = day or _now().date()
    since = _now() - timedelta(hours=window_hours)

    with session_scope() as db:
        candidates = _digest_candidates(db, since)

    if not candidates:
        logger.info("Digest run %s: no users with recent activity", day)
        return {"day": str(day), "candidates": 0, "sent": 0}

    results: dict[str, int] = {}
    # Sequential on purpose: each digest is an LLM call, and a burst of them
    # would compete with live user traffic for the same Mesh rate limit.
    for user_id in candidates:
        try:
            outcome = await _digest_for_user(user_id, day)
        except Exception:
            logger.exception("Digest failed for user %s", user_id)
            outcome = "error"
        results[outcome] = results.get(outcome, 0) + 1

    logger.info("Digest run %s: %d candidates, %s", day, len(candidates), results)
    return {"day": str(day), "candidates": len(candidates), "sent": results.get("sent", 0),
            "outcomes": results}


# --------------------------------------------------------------------------
# Housekeeping
# --------------------------------------------------------------------------
async def run_reconcile() -> dict | None:
    from app.services import catalog

    with session_scope() as db:
        report = await catalog.reconcile(db)
    if report.get("repaired") or report.get("orphans_deleted"):
        logger.info("Scheduled reconcile: %s", report)
    return report


def run_housekeeping() -> dict:
    """Expire stale recommendations and trim events past the retention window."""
    cutoff = _now() - timedelta(days=settings.event_retention_days)
    with session_scope() as db:
        expired = triggers.expire_stale(db)
        trimmed = db.execute(delete(Event).where(Event.server_ts < cutoff)).rowcount or 0
        db.commit()
    if expired or trimmed:
        logger.info("Housekeeping: %d recs expired, %d old events trimmed", expired, trimmed)
    return {"expired": expired, "trimmed": trimmed}


# --------------------------------------------------------------------------
# Wiring
# --------------------------------------------------------------------------
def _guard(name: str, coro_fn):
    """Wrap a job so one failure never kills the scheduler."""

    async def _run():
        try:
            result = coro_fn()
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            logger.exception("Scheduled job %r failed", name)

    return _run


def start_scheduler():
    """Start the scheduler and return it, so the lifespan can shut it down."""
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger

    scheduler = AsyncIOScheduler(timezone="UTC")

    scheduler.add_job(
        _guard("digest", run_digests),
        CronTrigger(hour=settings.digest_hour, minute=settings.digest_minute),
        id="daily_digest",
        # If the process was down at the trigger time, still send within the
        # hour rather than skipping the day entirely.
        misfire_grace_time=3600,
        coalesce=True,
        max_instances=1,
    )

    scheduler.add_job(
        _guard("reconcile", run_reconcile),
        IntervalTrigger(minutes=settings.reconcile_minutes),
        id="catalog_reconcile",
        misfire_grace_time=60,
        coalesce=True,
        max_instances=1,
    )

    scheduler.add_job(
        _guard("housekeeping", run_housekeeping),
        IntervalTrigger(hours=1),
        id="housekeeping",
        misfire_grace_time=300,
        coalesce=True,
        max_instances=1,
    )

    scheduler.start()
    logger.info(
        "Scheduler started: digest %02d:%02d UTC, reconcile every %dm, housekeeping hourly",
        settings.digest_hour, settings.digest_minute, settings.reconcile_minutes,
    )
    return scheduler


def next_run_times(scheduler) -> list[dict]:
    """Job schedule, for the admin view."""
    if scheduler is None:
        return []
    return [
        {"id": job.id, "next_run": getattr(job, "next_run_time", None), "trigger": str(job.trigger)}
        for job in scheduler.get_jobs()
    ]
