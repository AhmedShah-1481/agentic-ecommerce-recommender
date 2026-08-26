"""The brand facet.

`brand` replaced a three-value `level` enum as the metadata facet the agent
filters retrieval on, and that change is not just a rename: `level` was a closed
vocabulary the code could validate against a literal set, while `brand` is open
and grows every time an admin adds a product. Two failure modes come with that,
and both are silent - the slate just comes back thin or empty:

  * the model suggests a brand we don't stock, which builds a perfectly valid
    Chroma filter that matches nothing;
  * the model suggests a brand we *do* stock, but which has no products in the
    topical area the queries describe - also a valid filter matching nothing.

The first is caught before retrieval, the second only after it.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.agent import nodes
from app.config import settings
from app.db import Base


@pytest.fixture()
def configured(monkeypatch):
    """`analyze_behavior` returns its no-LLM fallback unless the client is
    configured, which would skip everything these tests are about."""
    monkeypatch.setattr(settings, "gemini_api_key", "test-key")
    return settings


def _profile() -> dict:
    return {
        "top_categories": [{"category": "Footwear", "score": 8.0}],
        "recent_searches": ["cushioned running shoes"],
        "most_engaged": [{"id": 1, "title": "Longhaul Cushioned Trainer"}],
        "typical_price": 140.0,
    }


def _analyse(monkeypatch, *, stocked: list[str], returns: dict) -> dict:
    """Run analyze_behavior with the catalog vocabulary and the model reply both
    pinned, so only the validation logic between them is under test."""
    monkeypatch.setattr(nodes.catalog, "brands", lambda db: stocked)

    async def fake_chat_json(messages, **kw):
        # Assert in passing that the vocabulary actually reached the prompt.
        fake_chat_json.prompt = messages[-1]["content"]
        return returns

    monkeypatch.setattr(nodes.gemini_client, "chat_json", fake_chat_json)
    result = asyncio.run(nodes.analyze_behavior({"profile": _profile()}))
    result["_prompt"] = fake_chat_json.prompt
    return result


# --------------------------------------------------------------------------
# Before retrieval: an invented brand never becomes a filter
# --------------------------------------------------------------------------
def test_a_stocked_brand_becomes_a_metadata_filter(configured, monkeypatch):
    result = _analyse(
        monkeypatch,
        stocked=["Kestrelane", "Marrowfield"],
        returns={
            "interests": ["running shoes"],
            "inferred_intent": "Replacing a worn pair of daily trainers.",
            "unmet_needs": ["running socks"],
            "queries": ["cushioned road running shoe"],
            "suggested_brand": "Kestrelane",
        },
    )

    assert result["metadata_filter"] == {"brand": "Kestrelane"}
    assert result["trace"][0]["brand_filter"] == "Kestrelane"


def test_a_brand_we_do_not_stock_is_dropped(configured, monkeypatch):
    """The regression the closed enum could not have: "Kestrelayne" is a
    plausible-looking hallucination that would filter the catalog down to
    nothing while looking like a working query."""
    result = _analyse(
        monkeypatch,
        stocked=["Kestrelane", "Marrowfield"],
        returns={
            "interests": ["running shoes"],
            "inferred_intent": "Replacing trainers.",
            "unmet_needs": [],
            "queries": ["cushioned road running shoe"],
            "suggested_brand": "Kestrelayne",
        },
    )

    assert result["metadata_filter"] == {}
    assert result["queries"] == ["cushioned road running shoe"], "queries survive the drop"


def test_the_stocked_vocabulary_is_given_to_the_model(configured, monkeypatch):
    """Validation alone would silently discard most suggestions. Listing the
    brands in the prompt is what makes a usable suggestion likely."""
    result = _analyse(
        monkeypatch,
        stocked=["Kestrelane", "Copperleaf"],
        returns={"queries": ["running shoes"], "suggested_brand": None},
    )

    assert "Kestrelane, Copperleaf" in result["_prompt"]


def test_a_null_brand_is_not_a_filter(configured, monkeypatch):
    """The prompt tells the model to prefer null over a weak guess, so null has
    to be the cheap path - not something that trips the validator."""
    result = _analyse(
        monkeypatch,
        stocked=["Kestrelane"],
        returns={"queries": ["running shoes"], "suggested_brand": None},
    )

    assert result["metadata_filter"] == {}


def test_an_unavailable_brand_vocabulary_does_not_lose_the_analysis(configured, monkeypatch):
    """A DB hiccup while reading the vocabulary must cost the filter, not the
    whole node - the queries are the part retrieval cannot proceed without."""
    def boom(db):
        raise RuntimeError("database is gone")

    monkeypatch.setattr(nodes.catalog, "brands", boom)

    async def fake_chat_json(messages, **kw):
        return {"queries": ["cushioned running shoe"], "suggested_brand": "Kestrelane"}

    monkeypatch.setattr(nodes.gemini_client, "chat_json", fake_chat_json)

    result = asyncio.run(nodes.analyze_behavior({"profile": _profile()}))

    assert result["queries"] == ["cushioned running shoe"]
    assert result["metadata_filter"] == {}, "no vocabulary means nothing to validate against"


# --------------------------------------------------------------------------
# After retrieval: a valid filter that matches nothing gets relaxed
# --------------------------------------------------------------------------
def _hit(pid: int, score: float, title: str, category: str) -> dict:
    """The shape store.query() returns: metadata is a nested dict, not flat."""
    return {
        "id": pid,
        "score": score,
        "distance": 1.0 - score,
        "metadata": {"title": title, "category": category},
        "document": title,
    }


def _retrieve(monkeypatch, *, hits_when_filtered: list[dict], hits_when_open: list[dict],
              metadata_filter: dict) -> dict:
    """Drive multi_query_retrieve with the vector store faked, returning one
    result set when a brand filter is present and another when it isn't."""
    calls: list[dict] = []

    async def fake_embed_one(text, **kw):
        return [0.1, 0.2, 0.3]

    def fake_query(vector, n_results, where):
        calls.append(where)
        filtered = "$and" in where
        return list(hits_when_filtered if filtered else hits_when_open)

    monkeypatch.setattr(nodes.embeddings, "embed_one", fake_embed_one)
    monkeypatch.setattr(nodes.store, "query", fake_query)
    # Hydration reads SQL for the ids the fake store returned, and it must not
    # reach the developer's on-disk database - whose schema may lag the models.
    # An empty in-memory one is enough: candidates are dropped at hydration, and
    # what these tests assert on is the where-clause and the relaxation flag,
    # both of which the trace carries.
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(nodes, "SessionLocal", sessionmaker(bind=engine))

    result = asyncio.run(
        nodes.multi_query_retrieve(
            {"queries": ["cushioned running shoe"], "metadata_filter": metadata_filter}
        )
    )
    result["_wheres"] = calls
    return result


def test_a_brand_filter_that_matches_nothing_is_retried_unfiltered(monkeypatch):
    """store.query() only falls back to an unfiltered search when the
    where-clause *raises*. A satisfiable-but-empty filter raises nothing, so
    without this retry the slate comes back empty and the refine loop burns its
    two attempts rewriting queries that were fine."""
    result = _retrieve(
        monkeypatch,
        hits_when_filtered=[],
        hits_when_open=[_hit(1, 0.4, "Longhaul Cushioned Trainer", "Footwear")],
        metadata_filter={"brand": "Copperleaf"},
    )

    assert result["_wheres"][0] == {"$and": [{"is_published": True},
                                             {"brand": "Copperleaf"}]}
    assert result["_wheres"][-1] == {"is_published": True}
    assert result["trace"][0]["filter_relaxed"] is True


def test_relaxing_clears_the_filter_so_the_reranker_stops_favouring_the_brand(monkeypatch):
    """`rerank_and_select` falls back to metadata_filter["brand"] for brand_fit.
    Leaving a just-disproved brand in state would keep giving it the tie-break
    bonus over brands that actually matched."""
    result = _retrieve(
        monkeypatch,
        hits_when_filtered=[],
        hits_when_open=[_hit(1, 0.4, "Trail Grip Off-Road Runner", "Footwear")],
        metadata_filter={"brand": "Copperleaf"},
    )

    assert result["metadata_filter"] == {}


def test_a_filter_that_matches_is_left_alone(monkeypatch):
    """The retry must not fire when the filter worked, or every filtered run
    would pay for a second fan-out."""
    result = _retrieve(
        monkeypatch,
        hits_when_filtered=[_hit(1, 0.4, "Tempo Pace Running Shoe", "Footwear")],
        hits_when_open=[_hit(2, 0.9, "Cast Iron Dutch Oven", "Home & Kitchen")],
        metadata_filter={"brand": "Kestrelane"},
    )

    assert result["_wheres"] == [{"$and": [{"is_published": True},
                                           {"brand": "Kestrelane"}]}]
    assert result["trace"][0]["filter_relaxed"] is False
    assert result["metadata_filter"] == {"brand": "Kestrelane"}


def test_an_unfiltered_run_that_finds_nothing_is_not_retried(monkeypatch):
    """No filter means nothing to relax; retrying would just repeat the same
    search and double the embedding spend on an empty catalog."""
    result = _retrieve(
        monkeypatch,
        hits_when_filtered=[],
        hits_when_open=[],
        metadata_filter={},
    )

    assert result["_wheres"] == [{"is_published": True}]
    assert result["trace"][0]["filter_relaxed"] is False
