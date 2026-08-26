"""Trigger gates - the answer to "don't call the LLM on every user action".

Every assertion here is a spend guarantee. A regression in this file doesn't
break the app; it just quietly multiplies the API bill, which is exactly why the
gates are tested rather than eyeballed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.db import Base
from app.models import Recommendation, User, UserProfileState
from app.services import triggers
from app.services.profile import ProfileSnapshot


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    session.add(User(id=1, email="a@b.dev", password_hash="x"))
    session.commit()
    try:
        yield session
    finally:
        session.close()


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _snapshot(signature: str = "sig-a", *, events: int = 20, high_signal: bool = False):
    snap = ProfileSnapshot(user_id=1, total_events=events)
    snap.category_affinity = {"MLOps": 5.0}
    snap.high_signal = high_signal
    snap.signature = signature
    return snap


def _existing_rec(db, *, signature: str = "sig-old", age_minutes: float = 60.0,
                  ttl_hours: float = 12.0) -> Recommendation:
    generated = _now() - timedelta(minutes=age_minutes)
    rec = Recommendation(
        user_id=1,
        headline="h",
        narrative="n",
        behavior_signature=signature,
        is_current=True,
        generated_at=generated,
        expires_at=generated + timedelta(hours=ttl_hours),
    )
    db.add(rec)
    db.commit()
    return rec


def _state(db, **kw) -> UserProfileState:
    state = triggers.get_state(db, 1)
    for key, value in kw.items():
        setattr(state, key, value)
    db.commit()
    return state


# --------------------------------------------------------------------------
# Gate: insufficient signal
# --------------------------------------------------------------------------
def test_a_user_who_has_done_nothing_costs_nothing(db):
    decision = triggers.evaluate(db, 1, _snapshot(events=1))

    assert decision.should_generate is False
    assert decision.reason == "insufficient_signal"


def test_first_recommendation_is_allowed_once_there_is_signal(db):
    decision = triggers.evaluate(db, 1, _snapshot())

    assert decision.should_generate is True
    assert decision.reason == "first_recommendation"


# --------------------------------------------------------------------------
# Gate: cooldown
# --------------------------------------------------------------------------
def test_cooldown_blocks_a_rapid_second_generation(db, monkeypatch):
    """The cooldown is pinned rather than read from the environment: `.env` may
    carry REC_COOLDOWN_MINUTES=1 for a live demo, and a 1-minute-old rec would
    then be *outside* the window and reach the momentum gate instead."""
    monkeypatch.setattr(settings, "rec_cooldown_minutes", 10)
    _existing_rec(db, age_minutes=1)
    _state(db, last_rec_at=_now() - timedelta(minutes=1), events_since_last_rec=100)

    decision = triggers.evaluate(db, 1, _snapshot("sig-new"))

    assert decision.should_generate is False
    assert decision.reason == "cooldown"


def test_generation_resumes_once_the_cooldown_expires(db):
    past = settings.rec_cooldown_minutes + 5
    _existing_rec(db, age_minutes=past)
    _state(db, last_rec_at=_now() - timedelta(minutes=past),
           events_since_last_rec=settings.rec_min_events)

    decision = triggers.evaluate(db, 1, _snapshot("sig-new"))

    assert decision.should_generate is True
    assert "event_momentum" in decision.reason


# --------------------------------------------------------------------------
# Gate: novelty (the one that saves the most money)
# --------------------------------------------------------------------------
def test_unchanged_behaviour_serves_the_cache(db):
    """Same signature means the model would write the same thing, so the answer
    is the stored one and no call is made - however much new activity there is."""
    _existing_rec(db, signature="sig-same", age_minutes=120)
    _state(db, last_rec_at=_now() - timedelta(minutes=120), events_since_last_rec=500)

    decision = triggers.evaluate(db, 1, _snapshot("sig-same"))

    assert decision.should_generate is False
    assert decision.reason == "behaviour_unchanged"


def test_novelty_is_checked_before_momentum(db):
    """Ordering matters: momentum alone would approve the 500 events above.
    Novelty has to be the earlier gate or the cache never gets used."""
    _existing_rec(db, signature="sig-same", age_minutes=120)
    _state(db, last_rec_at=_now() - timedelta(minutes=120), events_since_last_rec=9999)

    assert triggers.evaluate(db, 1, _snapshot("sig-same")).reason == "behaviour_unchanged"


# --------------------------------------------------------------------------
# Gate: momentum
# --------------------------------------------------------------------------
def test_a_trickle_of_activity_is_not_enough(db):
    _existing_rec(db, age_minutes=120)
    _state(db, last_rec_at=_now() - timedelta(minutes=120), events_since_last_rec=2)

    decision = triggers.evaluate(db, 1, _snapshot("sig-new"))

    assert decision.should_generate is False
    assert decision.reason.startswith("below_threshold")


def test_one_high_signal_action_can_bypass_the_event_count(db):
    """A 60-second dwell or a cart add says more than eight idle page views, so
    it is allowed to trigger early - but still needs *some* corroboration."""
    _existing_rec(db, age_minutes=120)
    _state(db, last_rec_at=_now() - timedelta(minutes=120), events_since_last_rec=2)

    decision = triggers.evaluate(db, 1, _snapshot("sig-new", high_signal=True))

    assert decision.should_generate is True
    assert decision.reason == "high_signal_action"


def test_an_expired_recommendation_refreshes_on_any_activity(db):
    generated = _now() - timedelta(hours=20)
    rec = _existing_rec(db, signature="sig-old", age_minutes=20 * 60, ttl_hours=1)
    assert rec.expires_at < _now()
    _state(db, last_rec_at=generated, events_since_last_rec=1)

    decision = triggers.evaluate(db, 1, _snapshot("sig-new"))

    assert decision.should_generate is True
    assert decision.reason == "stale_with_activity"


# --------------------------------------------------------------------------
# Gate: single-flight
# --------------------------------------------------------------------------
def test_an_in_flight_generation_blocks_a_second_one(db):
    _state(db, generating_since=_now())

    decision = triggers.evaluate(db, 1, _snapshot())

    assert decision.should_generate is False
    assert decision.reason == "already_generating"


def test_only_one_caller_wins_the_lease(db):
    """Two tabs, one generation. The conditional UPDATE decides the winner."""
    assert triggers.acquire_lease(db, 1) is True
    assert triggers.acquire_lease(db, 1) is False


def test_a_stale_lease_is_reclaimed(db):
    """A crashed worker must not lock a user out of recommendations forever."""
    _state(db, generating_since=_now() - triggers.LEASE_TIMEOUT - timedelta(seconds=1))

    assert triggers.acquire_lease(db, 1) is True


def test_releasing_the_lease_frees_it(db):
    triggers.acquire_lease(db, 1)
    triggers.release_lease(db, 1)

    assert triggers.acquire_lease(db, 1) is True


def test_force_still_respects_an_active_lease(db):
    """Force is for scheduled jobs and the manual refresh button; it overrides
    the *spend* gates, not the correctness one. Otherwise a refresh-clicking
    user could fan out into concurrent generations."""
    _state(db, generating_since=_now())

    decision = triggers.evaluate(db, 1, _snapshot(), force=True)

    assert decision.should_generate is False
    assert decision.reason == "already_generating"


def test_force_overrides_cooldown_and_novelty(db):
    _existing_rec(db, signature="sig-same", age_minutes=0)
    _state(db, last_rec_at=_now(), events_since_last_rec=0)

    decision = triggers.evaluate(db, 1, _snapshot("sig-same"), force=True)

    assert decision.should_generate is True
    assert decision.reason == "forced"


# --------------------------------------------------------------------------
# Bookkeeping
# --------------------------------------------------------------------------
def test_mark_generated_resets_the_counters(db):
    _state(db, events_since_last_rec=42, generating_since=_now())

    triggers.mark_generated(db, 1, "sig-new", ["MLOps", "Data Engineering"])
    state = triggers.get_state(db, 1)

    assert state.events_since_last_rec == 0
    assert state.last_signature == "sig-new"
    assert state.generating_since is None
    assert state.last_rec_at is not None


def test_expire_stale_demotes_only_past_ttl_recommendations(db):
    fresh = _existing_rec(db, age_minutes=1, ttl_hours=12)
    stale = Recommendation(
        user_id=1, headline="old", narrative="", is_current=True,
        generated_at=_now() - timedelta(hours=30),
        expires_at=_now() - timedelta(hours=18),
    )
    db.add(stale)
    db.commit()

    assert triggers.expire_stale(db) == 1
    assert fresh.is_current is True
    assert stale.is_current is False
