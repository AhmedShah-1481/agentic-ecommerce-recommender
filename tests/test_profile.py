"""Behavioural profile aggregation.

The decay maths decides which interests reach the prompt at all, and the
signature decides whether the LLM runs. Both are silent when wrong: nothing
throws, the recommendations just stop tracking the user (or start costing money
on every page view).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import Event, Product, User
from app.services import profile as profile_service


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")  # in-memory, per test
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.fixture()
def seeded(db):
    db.add(User(id=1, email="a@b.dev", password_hash="x"))
    db.add_all(
        [
            Product(id=1, title="Vector Search", description="", category="AI Engineering",
                    subcategory="RAG", level="advanced", price=100.0, tags=["rag"], rating=4.8),
            Product(id=2, title="Airflow Basics", description="", category="Data Engineering",
                    subcategory="Orchestration", level="beginner", price=80.0, tags=["airflow"],
                    rating=4.5),
        ]
    )
    db.commit()
    return db


def _event(eid: str, etype: str, *, product_id=None, hours_ago=0.0, **kw) -> Event:
    return Event(
        event_id=eid,
        user_id=1,
        event_type=etype,
        product_id=product_id,
        server_ts=_now() - timedelta(hours=hours_ago),
        search_query=kw.get("search_query", ""),
        dwell_ms=kw.get("dwell_ms", 0),
    )


# --------------------------------------------------------------------------
# Decay
# --------------------------------------------------------------------------
def test_decay_halves_every_half_life():
    assert profile_service._decay(0.0) == pytest.approx(1.0)
    assert profile_service._decay(profile_service.HALF_LIFE_HOURS) == pytest.approx(0.5)
    assert profile_service._decay(profile_service.HALF_LIFE_HOURS * 2) == pytest.approx(0.25)


def test_recent_interest_outranks_a_larger_pile_of_stale_interest(seeded):
    """The point of decay: three views today beat eight views last week.

    Without this the profile converges on a lifetime average and the
    recommendations stop reacting to what the user is doing right now.
    """
    for i in range(8):
        seeded.add(_event(f"old-{i}", "product_view", product_id=2, hours_ago=96))
    for i in range(3):
        seeded.add(_event(f"new-{i}", "product_view", product_id=1, hours_ago=0.5))
    seeded.commit()

    snapshot = profile_service.build_snapshot(seeded, 1)

    assert snapshot.top_categories[0] == "AI Engineering"
    assert snapshot.category_affinity["AI Engineering"] > snapshot.category_affinity[
        "Data Engineering"
    ]


def test_event_weight_ordering_reaches_the_score(seeded):
    """A search is worth more than a passive page view, and a cart add more
    than either - at equal age, so only the weights differ."""
    seeded.add(_event("v", "product_view", product_id=1))
    seeded.add(_event("c", "add_to_cart", product_id=2))
    seeded.commit()

    snapshot = profile_service.build_snapshot(seeded, 1)

    assert snapshot.category_affinity["Data Engineering"] > snapshot.category_affinity[
        "AI Engineering"
    ]


def test_long_dwell_marks_high_signal(seeded):
    seeded.add(_event("d", "dwell", product_id=1, dwell_ms=45_000))
    seeded.commit()

    assert profile_service.build_snapshot(seeded, 1).high_signal is True


def test_brief_dwell_is_not_high_signal(seeded):
    """A two-second glance must not unlock the high-signal trigger path -
    that gate is what lets one action bypass the event-count threshold."""
    seeded.add(_event("d", "dwell", product_id=1, dwell_ms=2_000))
    seeded.commit()

    assert profile_service.build_snapshot(seeded, 1).high_signal is False


def test_no_events_yields_no_signal(seeded):
    snapshot = profile_service.build_snapshot(seeded, 1)

    assert snapshot.total_events == 0
    assert snapshot.has_signal(3) is False
    assert snapshot.signature == ""


# --------------------------------------------------------------------------
# Signature - the cache key that decides whether the LLM runs
# --------------------------------------------------------------------------
def test_signature_is_stable_across_rebuilds(seeded):
    """Same behaviour must hash the same, or the novelty gate never holds and
    every page load pays for a regeneration."""
    seeded.add(_event("a", "product_view", product_id=1))
    seeded.add(_event("b", "search", search_query="rag"))
    seeded.commit()

    first = profile_service.build_snapshot(seeded, 1).signature
    second = profile_service.build_snapshot(seeded, 1).signature

    assert first and first == second


def test_signature_changes_when_the_user_moves_on(seeded):
    seeded.add(_event("a", "product_view", product_id=1))
    seeded.commit()
    before = profile_service.build_snapshot(seeded, 1).signature

    for i in range(5):
        seeded.add(_event(f"b-{i}", "product_view", product_id=2))
    seeded.commit()
    after = profile_service.build_snapshot(seeded, 1).signature

    assert before != after


def test_signature_ignores_pure_repetition(seeded):
    """Re-viewing the same course doesn't change *what* the user is interested
    in, so it must not invalidate the cache. This is the check that keeps a
    refresh-hammering tab from costing an LLM call per reload."""
    seeded.add(_event("a", "product_view", product_id=1))
    seeded.commit()
    before = profile_service.build_snapshot(seeded, 1).signature

    seeded.add(_event("b", "product_view", product_id=1))
    seeded.commit()
    after = profile_service.build_snapshot(seeded, 1).signature

    assert before == after


def test_searches_contribute_intent_without_a_product(seeded):
    seeded.add(_event("s1", "search", search_query="LangGraph agents"))
    seeded.commit()

    snapshot = profile_service.build_snapshot(seeded, 1)

    assert snapshot.recent_searches == ["LangGraph agents"]
    assert "langgraph" in snapshot.tag_affinity


def test_prompt_dict_stays_small(seeded):
    """The snapshot is the agent's entire view of the user; it has to stay a
    summary, not a transcript, or the prompt cost scales with browsing."""
    for i in range(60):
        seeded.add(_event(f"e-{i}", "product_view", product_id=(i % 2) + 1, hours_ago=i * 0.1))
    seeded.commit()

    payload = profile_service.build_snapshot(seeded, 1).to_prompt_dict()

    assert len(payload["top_categories"]) <= 5
    assert len(payload["most_engaged"]) <= 5
    assert len(payload["recent_searches"]) <= 6
    assert payload["total_tracked_events"] == 60
