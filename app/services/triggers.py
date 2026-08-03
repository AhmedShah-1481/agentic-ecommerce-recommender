"""Decides WHEN the recommendation agent is allowed to run.

This module is the answer to "don't fire an LLM call on every user action".
It contains no AI calls at all - only counters, timestamps and hashes - and
every gate must pass before a generation is dispatched:

  1. COOLDOWN     - a hard floor on generations per user per unit time.
  2. MOMENTUM     - enough new activity (or one high-signal action) to
                    plausibly change the answer.
  3. NOVELTY      - the behaviour signature must actually differ from the one
                    that produced the current recommendation. Same behaviour
                    means the same output, so we serve the cache instead.
  4. SINGLE-FLIGHT- a lease so concurrent tabs/requests can't fan out into
                    duplicate generations for one user.

Serving is always a plain indexed read; regeneration happens in the background
(stale-while-revalidate), so a page load never waits on the model.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Recommendation, UserProfileState
from app.services.profile import ProfileSnapshot

logger = logging.getLogger(__name__)

LEASE_TIMEOUT = timedelta(minutes=2)


@dataclass
class TriggerDecision:
    should_generate: bool
    reason: str

    def __bool__(self) -> bool:
        return self.should_generate


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def get_state(db: Session, user_id: int) -> UserProfileState:
    state = db.get(UserProfileState, user_id)
    if state is None:
        state = UserProfileState(user_id=user_id, events_since_last_rec=0)
        db.add(state)
        db.commit()
    return state


def current_recommendation(db: Session, user_id: int) -> Recommendation | None:
    return db.scalars(
        select(Recommendation)
        .where(Recommendation.user_id == user_id, Recommendation.is_current.is_(True))
        .order_by(Recommendation.generated_at.desc())
        .limit(1)
    ).first()


def evaluate(
    db: Session,
    user_id: int,
    snapshot: ProfileSnapshot,
    *,
    force: bool = False,
) -> TriggerDecision:
    """Should we spend an LLM call on this user right now?"""
    state = get_state(db, user_id)
    current = current_recommendation(db, user_id)
    now = _now()

    # -- gate 4 (checked first: cheapest, and protects against stampedes) --
    if state.generating_since is not None:
        if now - state.generating_since < LEASE_TIMEOUT:
            return TriggerDecision(False, "already_generating")
        logger.info("Reclaiming stale generation lease for user %s", user_id)

    if force:
        return TriggerDecision(True, "forced")

    # Not enough behaviour to personalise anything - don't pay for a guess.
    if not snapshot.has_signal(settings.rec_min_signal_events):
        return TriggerDecision(False, "insufficient_signal")

    if current is None:
        return TriggerDecision(True, "first_recommendation")

    # -- gate 1: cooldown -------------------------------------------------
    last = state.last_rec_at or current.generated_at
    if last is not None:
        elapsed = now - last
        if elapsed < timedelta(minutes=settings.rec_cooldown_minutes):
            return TriggerDecision(False, "cooldown")

    # -- gate 3: novelty --------------------------------------------------
    # Cheap and decisive: identical behaviour would yield identical copy.
    if snapshot.signature and snapshot.signature == current.behavior_signature:
        return TriggerDecision(False, "behaviour_unchanged")

    # -- gate 2: momentum -------------------------------------------------
    events_since = state.events_since_last_rec or 0
    expired = current.expires_at is not None and current.expires_at <= now

    if events_since >= settings.rec_min_events:
        return TriggerDecision(True, f"event_momentum({events_since})")
    if snapshot.high_signal and events_since >= 2:
        return TriggerDecision(True, "high_signal_action")
    if expired and events_since >= 1:
        return TriggerDecision(True, "stale_with_activity")

    return TriggerDecision(False, f"below_threshold({events_since}/{settings.rec_min_events})")


# --------------------------------------------------------------------------
# Lease handling (single-flight)
# --------------------------------------------------------------------------
def acquire_lease(db: Session, user_id: int) -> bool:
    """Claim the right to generate. False if someone else already holds it.

    A single conditional UPDATE, not read-then-write: with two workers the
    read-then-write shape lets both see a free lease and both claim it, which
    defeats the whole point of the gate. The WHERE clause makes the winner
    whoever's UPDATE lands first, and the loser sees rowcount 0.
    """
    get_state(db, user_id)  # ensure the row exists
    now = _now()
    cutoff = now - LEASE_TIMEOUT

    result = db.execute(
        update(UserProfileState)
        .where(
            UserProfileState.user_id == user_id,
            or_(
                UserProfileState.generating_since.is_(None),
                UserProfileState.generating_since <= cutoff,  # stale, reclaim it
            ),
        )
        .values(generating_since=now)
    )
    db.commit()
    return result.rowcount > 0


def release_lease(db: Session, user_id: int) -> None:
    state = db.get(UserProfileState, user_id)
    if state is not None:
        state.generating_since = None
        db.commit()


def mark_generated(db: Session, user_id: int, signature: str, categories: list[str]) -> None:
    """Reset the counters that gate the next generation."""
    state = get_state(db, user_id)
    state.events_since_last_rec = 0
    state.last_rec_at = _now()
    state.last_signature = signature
    state.top_categories = categories[:5]
    state.generating_since = None
    db.commit()


def expire_stale(db: Session) -> int:
    """Mark past-TTL recommendations non-current. Run hourly by the scheduler."""
    now = _now()
    stale = list(
        db.scalars(
            select(Recommendation).where(
                Recommendation.is_current.is_(True),
                Recommendation.expires_at.isnot(None),
                Recommendation.expires_at <= now,
            )
        )
    )
    for rec in stale:
        rec.is_current = False
    if stale:
        db.commit()
    return len(stale)
