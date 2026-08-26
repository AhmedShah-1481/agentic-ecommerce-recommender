"""The grounding guard.

This node is what makes "grounded in the real catalog" a property rather than a
hope. Every assertion here is about the same failure: a model that names a
product we don't stock, or attaches a reason to an id that was never retrieved.
"""

from __future__ import annotations

import asyncio

from app.agent import nodes


def _run(state: dict) -> dict:
    return asyncio.run(nodes.validate_grounding(state))


def _selected() -> list[dict]:
    return [
        {"id": 7, "title": "Longhaul Cushioned Trainer", "category": "Footwear"},
        {"id": 9, "title": "Grid Fleece Half-Zip", "category": "Apparel"},
    ]


def test_reasons_for_ids_that_were_never_retrieved_are_dropped():
    """The headline case: the model returns a plausible id that isn't in the
    slate. It must not reach storage, because RecommendationItem would then
    point at a product the user was never actually matched to."""
    result = _run(
        {
            "selected": _selected(),
            "item_reasons": {7: "Matches the trainers you kept returning to.", 404: "Invented product."},
            "narrative": "Two picks for you.",
            "headline": "Your next step",
        }
    )

    assert set(result["item_reasons"]) == {7, 9}
    assert 404 not in result["item_reasons"]


def test_the_drop_is_recorded_in_the_trace():
    """Silently dropping hallucinations would hide how often the model does it.
    /admin/traces has to be able to show it."""
    result = _run(
        {
            "selected": _selected(),
            "item_reasons": {7: "ok", 404: "invented", 405: "also invented"},
            "narrative": "",
            "headline": "h",
        }
    )

    entry = result["trace"][0]
    assert entry["dropped"] == 2
    assert "dropped_2_invented_ids" in entry["issues"]


def test_every_selected_product_ends_up_with_a_reason():
    """A card with an empty "why" reads like a bug to the user, so a missing or
    blank reason is backfilled from the product's own category - still grounded,
    just less specific."""
    result = _run(
        {
            "selected": _selected(),
            "item_reasons": {7: "   "},
            "narrative": "",
            "headline": "h",
        }
    )

    assert result["item_reasons"][7].strip()
    assert result["item_reasons"][9].strip()
    assert "Apparel" in result["item_reasons"][9]


def test_a_narrative_quoting_a_product_we_do_not_stock_is_flagged():
    result = _run(
        {
            "selected": _selected(),
            "item_reasons": {},
            "narrative": 'Start with "Merino Wool Crewneck Sweater" to go deeper.',
            "headline": "h",
        }
    )

    assert "narrative_cites_unknown_title" in result["trace"][0]["issues"]


def test_a_narrative_quoting_a_selected_product_is_clean():
    result = _run(
        {
            "selected": _selected(),
            "item_reasons": {},
            "narrative": 'Start with "Longhaul Cushioned Trainer" to go deeper.',
            "headline": "h",
        }
    )

    assert result["trace"][0]["issues"] == ["none"]


def test_quoted_title_check_is_case_insensitive():
    result = _run(
        {
            "selected": _selected(),
            "item_reasons": {},
            "narrative": 'Try "longhaul cushioned trainer" next.',
            "headline": "h",
        }
    )

    assert result["trace"][0]["issues"] == ["none"]


def test_short_quoted_phrases_are_not_treated_as_titles():
    """Models quote ordinary words. Flagging "merino" as a hallucinated title
    would make the signal useless."""
    result = _run(
        {
            "selected": _selected(),
            "item_reasons": {},
            "narrative": 'You keep returning to "merino" and "wool".',
            "headline": "h",
        }
    )

    assert result["trace"][0]["issues"] == ["none"]


def test_an_empty_headline_is_replaced():
    result = _run(
        {"selected": _selected(), "item_reasons": {}, "narrative": "n", "headline": "  "}
    )

    assert result["headline"] == "Picked for you"


def test_an_empty_slate_is_survivable():
    """Nothing to validate must not raise - the cold-start path routes here too."""
    result = _run({"selected": [], "item_reasons": {3: "x"}, "narrative": "", "headline": ""})

    assert result["item_reasons"] == {}
    assert result["headline"] == "Picked for you"


def test_the_narrative_itself_is_passed_through_unchanged():
    """The guard prunes and flags; it never rewrites copy. Regeneration is the
    graph's job, not this node's."""
    narrative = "You spent 4 minutes comparing cushioned trainers this morning."
    result = _run(
        {"selected": _selected(), "item_reasons": {}, "narrative": narrative, "headline": "h"}
    )

    assert result["narrative"] == narrative
