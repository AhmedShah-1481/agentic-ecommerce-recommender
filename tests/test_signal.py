"""The "Your Signal" feed.

Display layer, but two things here are load-bearing: the chips must describe the
*same* weighting the profile actually scores with (otherwise the panel lies
about how the recommender works), and the query must never return one user's
behaviour to another.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import Event, Product, User, UserProfileState
from app.services import events as events_service
from app.services import profile as profile_service
from app.services import signal as signal_service


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
    db.add_all(
        [
            User(id=1, email="a@b.dev", password_hash="x"),
            User(id=2, email="other@b.dev", password_hash="x"),
        ]
    )
    db.add(
        Product(id=1, title="Vector Search at Scale", description="", category="AI Engineering",
                subcategory="RAG", level="advanced", price=100.0, tags=["rag"], rating=4.8)
    )
    db.commit()
    return db


def _event(eid: str, etype: str, *, user_id: int = 1, product_id=None,
           minutes_ago: float = 0.0, **kw) -> Event:
    return Event(
        event_id=eid,
        user_id=user_id,
        event_type=etype,
        product_id=product_id,
        server_ts=_now() - timedelta(minutes=minutes_ago),
        search_query=kw.get("search_query", ""),
        dwell_ms=kw.get("dwell_ms", 0),
        scroll_pct=kw.get("scroll_pct", 0),
        path=kw.get("path", ""),
        meta=kw.get("meta", {}),
    )


# --------------------------------------------------------------------------
# Isolation - the one place a display bug would become a privacy bug
# --------------------------------------------------------------------------
def test_a_user_never_sees_another_users_events(seeded):
    seeded.add(_event("mine", "product_view", user_id=1, product_id=1))
    seeded.add(_event("theirs", "search", user_id=2, search_query="secret interest"))
    seeded.commit()

    mine = signal_service.recent(seeded, 1)
    theirs = signal_service.recent(seeded, 2)

    assert [c["detail"] for c in mine] == ["Vector Search at Scale"]
    assert [c["detail"] for c in theirs] == ["secret interest"]


def test_a_user_with_no_events_gets_an_empty_feed(seeded):
    assert signal_service.recent(seeded, 1) == []


# --------------------------------------------------------------------------
# Ordering and bounds
# --------------------------------------------------------------------------
def test_newest_event_comes_first(seeded):
    seeded.add(_event("old", "search", search_query="older", minutes_ago=30))
    seeded.add(_event("new", "search", search_query="newer", minutes_ago=1))
    seeded.commit()

    assert [c["detail"] for c in signal_service.recent(seeded, 1)] == ["newer", "older"]


def test_the_limit_is_honoured(seeded):
    for i in range(12):
        seeded.add(_event(f"e-{i}", "product_view", product_id=1, minutes_ago=i))
    seeded.commit()

    assert len(signal_service.recent(seeded, 1, limit=5)) == 5
    assert len(signal_service.recent(seeded, 1)) == signal_service.DEFAULT_LIMIT


def test_the_limit_is_clamped_so_a_query_string_cannot_dump_the_table(seeded):
    """`limit` arrives from the URL, so an unbounded value would let anyone turn
    a polled endpoint into a full history export."""
    for i in range(40):
        seeded.add(_event(f"e-{i}", "product_view", product_id=1, minutes_ago=i))
    seeded.commit()

    assert len(signal_service.recent(seeded, 1, limit=10_000)) == signal_service.MAX_LIMIT
    assert len(signal_service.recent(seeded, 1, limit=0)) == signal_service.DEFAULT_LIMIT
    assert len(signal_service.recent(seeded, 1, limit=-3)) >= 1


def test_page_view_is_excluded(seeded):
    """It carries no product and fires on every navigation - including the
    dashboard the panel lives on, which would crowd out real engagement."""
    seeded.add(_event("pv", "page_view", path="/dashboard"))
    seeded.add(_event("view", "product_view", product_id=1, minutes_ago=5))
    seeded.commit()

    types = [c["type"] for c in signal_service.recent(seeded, 1)]
    assert types == ["product_view"]


# --------------------------------------------------------------------------
# Chip wording
# --------------------------------------------------------------------------
def test_a_search_shows_the_query(seeded):
    chip = signal_service.describe(_event("s", "search", search_query="agentic ai"))

    assert chip["verb"] == "Searched"
    assert chip["detail"] == "agentic ai"


def test_a_dwell_shows_duration_and_course(seeded):
    seeded.add(_event("d", "dwell", product_id=1, dwell_ms=12_000))
    seeded.commit()

    chip = signal_service.recent(seeded, 1)[0]
    assert chip["verb"] == "Dwell"
    assert chip["detail"] == "12s on Vector Search at Scale"


def test_a_long_dwell_is_rendered_in_minutes(seeded):
    chip = signal_service.describe(_event("d", "dwell", dwell_ms=95_000))

    assert chip["detail"] == "1m 35s"


def test_a_whole_minute_dwell_omits_the_seconds(seeded):
    assert signal_service.describe(_event("d", "dwell", dwell_ms=120_000))["detail"] == "2m"


def test_scroll_depth_shows_a_percentage(seeded):
    chip = signal_service.describe(_event("sc", "scroll_depth", scroll_pct=75))

    assert chip["verb"] == "Scrolled"
    assert "75%" in chip["detail"]


def test_an_unknown_event_type_degrades_to_readable_words(seeded):
    """A new event type added to the tracker must not render as a raw key or
    crash the panel."""
    chip = signal_service.describe(_event("x", "video_play", product_id=None))

    assert chip["verb"] == "video play"


# --------------------------------------------------------------------------
# Catalog facets ride on `search`, but they are not typed queries
# --------------------------------------------------------------------------
def test_a_category_filter_reads_as_filtered_not_searched(seeded):
    """Choosing a category is emitted as a `search` so it reaches the momentum
    gate and the agent's retrieval queries without a new event type. The user
    still didn't type anything, so the panel must not say they did."""
    chip = signal_service.describe(
        _event("f", "search", search_query="Web Development", meta={"source": "filter"})
    )

    assert chip["verb"] == "Filtered"
    assert chip["detail"] == "Web Development"


def test_a_typed_search_is_still_searched(seeded):
    chip = signal_service.describe(_event("s", "search", search_query="agentic ai", meta={}))

    assert chip["verb"] == "Searched"


def test_a_filter_carries_the_same_weight_as_a_search(seeded):
    """The relabelling is wording only - it must not imply a different score
    from the one profile.py actually applies."""
    chip = signal_service.describe(
        _event("f", "search", search_query="Design", meta={"source": "filter"})
    )

    assert chip["weight"] == profile_service.EVENT_WEIGHTS["search"]


def test_a_facet_survives_the_real_ingest_path(seeded):
    """The whole fix hinges on `meta` making it through ingest intact: the
    tracker sends it, but if the batch writer dropped it the chip would silently
    read "Searched" and the momentum counter is the other half of the claim.
    Goes through events._write_batch rather than constructing an Event, so this
    breaks if the real write path stops carrying either one."""
    events_service._write_batch(
        seeded,
        [
            {
                "event_id": "facet-1",
                "user_id": 1,
                "event_type": "search",
                "search_query": "Web Development",
                "meta": {"source": "filter"},
            }
        ],
    )

    chip = signal_service.recent(seeded, 1)[0]
    assert chip["verb"] == "Filtered"
    assert chip["detail"] == "Web Development"

    # `search` is in MEANINGFUL_TYPES, so the facet moves the momentum gate -
    # which a page_view (what this navigation used to produce) never did.
    state = seeded.get(UserProfileState, 1)
    assert state.events_since_last_rec == 1


# --------------------------------------------------------------------------
# "Strong" must track profile.py, not a second opinion
# --------------------------------------------------------------------------
def test_add_to_cart_is_marked_strong_and_carries_the_real_weight(seeded):
    seeded.add(_event("c", "add_to_cart", product_id=1))
    seeded.commit()

    chip = signal_service.recent(seeded, 1)[0]
    assert chip["strong"] is True
    assert chip["verb"] == "Added to cart"
    # Imported from profile.py rather than restated, so this pins that the panel
    # reports the weighting the recommender actually applies.
    assert chip["weight"] == profile_service.EVENT_WEIGHTS["add_to_cart"]


def test_a_dwell_past_the_long_threshold_is_strong(seeded):
    long_dwell = signal_service.describe(
        _event("d", "dwell", dwell_ms=profile_service.LONG_DWELL_MS)
    )
    brief = signal_service.describe(_event("d", "dwell", dwell_ms=2_000))

    assert long_dwell["strong"] is True
    assert brief["strong"] is False


def test_a_plain_view_is_not_strong(seeded):
    assert signal_service.describe(_event("v", "product_view", product_id=1))["strong"] is False
