"""Reranking guarantees.

The reranker is pure arithmetic with no LLM, which makes it exactly the place
a subtle weighting bug hides: nothing crashes, the slate just quietly drifts
off-topic. These pin the properties that regressed during the build.
"""

from __future__ import annotations

from app.agent import nodes


def _candidate(pid: int, title: str, category: str, score: float, **kw) -> dict:
    return {
        "id": pid,
        "title": title,
        "category": category,
        "score": score,
        "rrf": kw.get("rrf", 1.0 / (nodes.RRF_K + 1)),
        "matched_queries": kw.get("matched_queries", 1),
        "level": kw.get("level", "intermediate"),
        "price": kw.get("price", 100.0),
        "rating": kw.get("rating", 4.5),
    }


def _state(candidates, **kw) -> dict:
    return {
        "candidates": candidates,
        "profile": {
            "top_categories": kw.get("top_categories", [{"category": "MLOps", "score": 10.0}]),
            "typical_price": kw.get("typical_price"),
        },
        "seen_product_ids": kw.get("seen", []),
        "preferred_level": kw.get("preferred_level", "intermediate"),
    }


def test_topical_fit_beats_hygiene_factors():
    """An unseen, perfectly-levelled, top-rated but off-topic course must not
    outrank an on-topic one the user already opened.

    This is the Figma-for-an-MLOps-learner regression: with additive weights the
    hygiene terms (novelty + level + rating + price) were worth more than
    category affinity, so irrelevance was survivable if the metadata was tidy.
    """
    on_topic_seen = _candidate(1, "Model Monitoring", "MLOps", 0.30)
    off_topic_fresh = _candidate(2, "Design Systems in Figma", "Design", 0.20, rating=5.0)

    result = nodes.rerank_and_select(_state([on_topic_seen, off_topic_fresh], seen=[1]))

    assert result["selected"][0]["id"] == 1
    assert on_topic_seen["final_score"] > off_topic_fresh["final_score"]


def test_seen_is_a_discount_not_a_veto():
    """Already-opened courses stay eligible - "continue where you left off" is a
    legitimate recommendation, just a weaker one than something new."""
    seen = _candidate(1, "Airflow in Production", "MLOps", 0.30)
    fresh = _candidate(2, "Airflow in Production II", "MLOps", 0.30)

    nodes.rerank_and_select(_state([seen, fresh], seen=[1]))

    assert fresh["final_score"] > seen["final_score"]
    assert seen["final_score"] > 0.8 * fresh["final_score"], "penalty should be mild"


def test_scoring_is_invariant_to_similarity_scale():
    """Ranking must not change when the embedding provider compresses the
    similarity range. The local fallback emits ~0.08-0.30 where a trained model
    emits ~0.3-0.9; switching providers must not reshuffle the slate."""
    wide = [_candidate(1, "A", "MLOps", 0.90), _candidate(2, "B", "MLOps", 0.60),
            _candidate(3, "C", "Design", 0.30)]
    narrow = [_candidate(1, "A", "MLOps", 0.30), _candidate(2, "B", "MLOps", 0.20),
              _candidate(3, "C", "Design", 0.10)]

    wide_order = [c["id"] for c in nodes.rerank_and_select(_state(wide))["selected"]]
    narrow_order = [c["id"] for c in nodes.rerank_and_select(_state(narrow))["selected"]]

    assert wide_order == narrow_order


def test_below_band_filler_is_not_used_to_pad_a_healthy_slate():
    """Two strong matches plus junk should return the strong ones only.

    Padding to rec_max_items with off-band items made the narrative hedge
    ("if you choose to step outside your pattern"), which undercuts the
    grounded reasons sitting next to it.
    """
    candidates = [
        _candidate(1, "Model Monitoring", "MLOps", 0.30),
        _candidate(2, "Feature Stores", "MLOps", 0.29),
        _candidate(3, "Interaction Design", "Design", 0.02),
        _candidate(4, "UX Research", "Design", 0.01),
    ]

    selected = nodes.rerank_and_select(_state(candidates))["selected"]

    assert {c["id"] for c in selected} == {1, 2}
    assert all(not c.get("off_band") for c in selected)


def test_category_cap_spreads_the_slate_when_it_can():
    """With enough on-band candidates across categories, the cap holds."""
    candidates = []
    for c_i, category in enumerate(["MLOps", "Data Engineering", "LLM Engineering", "RAG & Search"]):
        for i in range(3):
            candidates.append(
                _candidate(c_i * 10 + i, f"{category} {i}", category, 0.30 - i * 0.001)
            )

    selected = nodes.rerank_and_select(
        _state(candidates, top_categories=[
            {"category": "MLOps", "score": 10.0},
            {"category": "Data Engineering", "score": 9.0},
        ])
    )["selected"]

    per_category: dict[str, int] = {}
    for candidate in selected:
        per_category[candidate["category"]] = per_category.get(candidate["category"], 0) + 1
    assert per_category and max(per_category.values()) <= nodes.MAX_PER_CATEGORY


def test_cap_is_relaxed_before_relevance_is():
    """When too few categories are on-band, prefer a third strong on-topic
    course over reaching below the relevance band.

    Two categories with a cap of 2 can only fill 3 of 4 slots, so the ladder
    has to give somewhere. Exceeding the variety cap is the cheaper concession:
    the extra pick is still something the user demonstrably cares about.
    """
    candidates = [_candidate(i, f"MLOps {i}", "MLOps", 0.30 - i * 0.001) for i in range(6)]
    candidates.append(_candidate(99, "Airflow", "Data Engineering", 0.28))
    # Plainly off-topic, far below the band - must never be reached for.
    candidates += [_candidate(200 + i, f"UX {i}", "Design", 0.01) for i in range(2)]

    selected = nodes.rerank_and_select(
        _state(candidates, top_categories=[
            {"category": "MLOps", "score": 10.0},
            {"category": "Data Engineering", "score": 9.0},
        ])
    )["selected"]

    assert len([c for c in selected if c["category"] == "MLOps"]) > nodes.MAX_PER_CATEGORY
    assert any(c["id"] == 99 for c in selected)
    assert not any(c["category"] == "Design" for c in selected)


def test_empty_candidates_is_survivable():
    assert nodes.rerank_and_select(_state([]))["selected"] == []
