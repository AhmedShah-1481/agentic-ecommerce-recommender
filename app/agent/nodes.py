"""LangGraph nodes for the recommendation agent.

Flow (see graph.py for wiring):

    analyze_behavior -> decide_retrieval -+-> (skip) -------> END
                                          |
                                          +-> multi_query_retrieve
                                                    |
                                              grade_relevance
                                                |         |
                                    (weak) refine_query   | (good)
                                                |         v
                                                +--> rerank_and_select
                                                          |
                                                   generate_narrative
                                                          |
                                                   validate_grounding
                                                          |
                                                         END

Every node appends a trace entry, so the whole run is inspectable at
/admin/traces even without LangSmith configured.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from sqlalchemy import select

from app.agent import prompts
from app.agent.state import AgentState
from app.ai import embeddings
from app.ai.gemini_client import gemini_client
from app.config import settings
from app.db import SessionLocal
from app.models import Product
from app.services import catalog
from app.vector import store

logger = logging.getLogger(__name__)

MAX_REFINE_ATTEMPTS = 2
GOOD_ENOUGH_SCORE = 0.30
CANDIDATES_PER_QUERY = 8
RRF_K = 60

# Re-ranking / selection policy.
MAX_PER_CATEGORY = 2      # variety without derailing relevance
RELEVANCE_BAND = 0.55     # keep candidates scoring >=55% of the best match
MIN_RELEVANCE_SCORE = 0.05
MIN_SLATE = 2             # below-band filler only to avoid a bare slate


def _trace(node: str, started: float, **detail) -> list[dict]:
    return [{"node": node, "ms": int((time.perf_counter() - started) * 1000), **detail}]


# --------------------------------------------------------------------------
# 1. Analyse behaviour
# --------------------------------------------------------------------------
async def analyze_behavior(state: AgentState) -> dict:
    started = time.perf_counter()
    profile = state.get("profile") or {}
    seen_titles = [p.get("title", "") for p in profile.get("most_engaged", [])]

    fallback = {
        "interests": [c["category"] for c in profile.get("top_categories", [])[:3]],
        "inferred_intent": "",
        "unmet_needs": [],
        "queries": [],
        "metadata_filter": {},
        "used_llm": False,
    }
    # Even with no LLM we can still retrieve using raw behaviour.
    fallback["queries"] = [
        c["category"] for c in profile.get("top_categories", [])[:2]
    ] + profile.get("recent_searches", [])[:2]

    if not settings.gemini_configured:
        return {
            **fallback,
            "trace": _trace("analyze_behavior", started, skipped="gemini_not_configured"),
        }

    # The brand facet is open-vocabulary, so the stocked list is both given to
    # the model and used to validate what comes back.
    db = SessionLocal()
    try:
        known_brands = catalog.brands(db)
    except Exception:  # noqa: BLE001 - a DB hiccup here must not lose the analysis
        logger.warning("Could not load brand vocabulary", exc_info=True)
        known_brands = []
    finally:
        db.close()

    try:
        data = await gemini_client.chat_json(
            [
                {"role": "system", "content": prompts.ANALYSE_SYSTEM},
                {
                    "role": "user",
                    "content": prompts.analyse_user_prompt(
                        profile, seen_titles, brands=known_brands
                    ),
                },
            ],
            purpose="agent_analyze",
            user_id=state.get("user_id"),
            temperature=0.3,
            max_tokens=1400,
            response_schema=prompts.ANALYSE_SCHEMA,
        )
    except Exception as exc:  # noqa: BLE001 - GeminiError/timeouts: degrade, never crash
        logger.warning("analyze_behavior failed, using behavioural fallback: %s", exc)
        return {
            **fallback,
            "trace": _trace("analyze_behavior", started, error=str(exc)[:200]),
        }

    queries = [q for q in (data.get("queries") or []) if isinstance(q, str) and q.strip()]
    interests = [i for i in (data.get("interests") or []) if isinstance(i, str)]

    # Validated against the real catalog vocabulary. A brand we don't stock would
    # still build a *valid* Chroma filter - one that matches nothing - so an
    # unrecognised suggestion is dropped rather than allowed to empty the slate.
    suggested = data.get("suggested_brand")
    metadata_filter: dict[str, Any] = {}
    if isinstance(suggested, str) and suggested in known_brands:
        metadata_filter["brand"] = suggested

    return {
        "interests": interests[:5],
        "inferred_intent": str(data.get("inferred_intent") or "")[:300],
        "unmet_needs": [g for g in (data.get("unmet_needs") or []) if isinstance(g, str)][:3],
        "queries": queries[:4] or fallback["queries"],
        "metadata_filter": metadata_filter,
        "used_llm": True,
        "trace": _trace(
            "analyze_behavior",
            started,
            interests=interests[:5],
            intent=str(data.get("inferred_intent") or "")[:120],
            queries=queries[:4],
            brand_filter=metadata_filter.get("brand") or "",
        ),
    }


# --------------------------------------------------------------------------
# 2. Decide whether retrieval is worth it (a real branch, not decoration)
# --------------------------------------------------------------------------
def decide_retrieval(state: AgentState) -> str:
    profile = state.get("profile") or {}
    has_queries = bool(state.get("queries"))
    has_signal = bool(profile.get("top_categories")) or bool(profile.get("recent_searches"))
    return "retrieve" if (has_queries and has_signal) else "skip"


async def cold_start(state: AgentState) -> dict:
    """No usable behaviour: return top-rated published products, zero LLM spend."""
    started = time.perf_counter()
    db = SessionLocal()
    try:
        rows = list(
            db.scalars(
                select(Product)
                .where(Product.is_published.is_(True))
                .order_by(Product.rating.desc())
                .limit(settings.rec_max_items)
            )
        )
        selected = [
            {
                "id": p.id,
                "title": p.title,
                "category": p.category,
                "subcategory": p.subcategory,
                "brand": p.brand,
                "size": p.size,
                "color": p.color,
                "price": float(p.price or 0),
                "description": p.description,
                "score": 0.0,
                "final_score": 0.0,
            }
            for p in rows
        ]
    finally:
        db.close()

    return {
        "selected": selected,
        "is_fallback": True,
        "headline": "Start with our best-rated pieces",
        "narrative": (
            "You're new here, so here's what our customers rate most highly. "
            "Browse a few and your recommendations will sharpen to match your taste."
        ),
        "item_reasons": {p["id"]: "Consistently top-rated by customers." for p in selected},
        "trace": _trace("cold_start", started, count=len(selected)),
    }


# --------------------------------------------------------------------------
# 3. Multi-query retrieval with metadata filtering + reciprocal rank fusion
# --------------------------------------------------------------------------
async def _fan_out(queries: list[str], where: dict[str, Any]) -> tuple[dict[int, dict], list[dict]]:
    """Embed each query, search, and fuse the rank lists. Returns (fused, trace)."""
    fused: dict[int, dict] = {}
    per_query: list[dict] = []

    for query in queries:
        try:
            # Embedded as a QUERY, not a document. Gemini builds an asymmetric
            # pair - `RETRIEVAL_QUERY` vectors are optimised to land near the
            # `RETRIEVAL_DOCUMENT` vectors catalog.py stored - and getting this
            # backwards costs relevance without raising anything.
            vector = await embeddings.embed_one(
                query, purpose="agent_retrieve", task=embeddings.TASK_QUERY
            )
            hits = store.query(vector, n_results=CANDIDATES_PER_QUERY, where=where)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Retrieval failed for query %r: %s", query, exc)
            per_query.append({"query": query, "hits": 0, "error": str(exc)[:120]})
            continue

        per_query.append(
            {"query": query, "hits": len(hits), "top": hits[0]["metadata"].get("title") if hits else None}
        )

        # Reciprocal rank fusion: robust to score-scale differences between
        # queries, and rewards products that surface for several intents.
        for position, hit in enumerate(hits):
            pid = hit["id"]
            entry = fused.setdefault(
                pid,
                {
                    "id": pid,
                    "rrf": 0.0,
                    "best_score": 0.0,
                    "matched_queries": 0,
                    "metadata": hit["metadata"],
                },
            )
            entry["rrf"] += 1.0 / (RRF_K + position + 1)
            entry["best_score"] = max(entry["best_score"], hit["score"])
            entry["matched_queries"] += 1

    return fused, per_query


async def multi_query_retrieve(state: AgentState) -> dict:
    started = time.perf_counter()
    queries = state.get("queries") or []
    metadata_filter = dict(state.get("metadata_filter") or {})

    where: dict[str, Any] = {"is_published": True}
    # Chroma needs an explicit $and for multiple conditions.
    if metadata_filter.get("brand"):
        where = {
            "$and": [
                {"is_published": True},
                {"brand": metadata_filter["brand"]},
            ]
        }

    fused, per_query = await _fan_out(queries, where)

    # A brand filter is a hard prune, and unlike the enum it replaced it can be
    # satisfiable in the catalog yet empty in the user's topical area - a valid
    # filter matching nothing. store.query() only retries unfiltered when the
    # where-clause *raises*, so without this the slate would silently come back
    # empty and the run would burn its refine attempts on good queries.
    relaxed = False
    if not fused and metadata_filter.get("brand"):
        logger.info("Brand-filtered retrieval was empty; retrying unfiltered")
        relaxed = True
        where = {"is_published": True}
        fused, per_query = await _fan_out(queries, where)

    candidates = sorted(fused.values(), key=lambda c: -c["rrf"])[:20]

    # Hydrate from SQL - the vector store holds metadata, not the source of truth.
    if candidates:
        db = SessionLocal()
        try:
            products = {
                p.id: p
                for p in db.scalars(select(Product).where(Product.id.in_([c["id"] for c in candidates])))
            }
        finally:
            db.close()

        hydrated = []
        for candidate in candidates:
            product = products.get(candidate["id"])
            if product is None or not product.is_published:
                continue  # vector drift - SQL wins
            hydrated.append(
                {
                    "id": product.id,
                    "title": product.title,
                    "description": product.description,
                    "category": product.category,
                    "subcategory": product.subcategory,
                    "brand": product.brand,
                    "size": product.size,
                    "color": product.color,
                    "price": float(product.price or 0),
                    "rating": float(product.rating or 0),
                    "tags": product.tags or [],
                    "score": candidate["best_score"],
                    "rrf": candidate["rrf"],
                    "matched_queries": candidate["matched_queries"],
                }
            )
        candidates = hydrated

    quality = max((c["score"] for c in candidates), default=0.0)

    return {
        "candidates": candidates,
        "retrieval_quality": quality,
        # Cleared when relaxed so the reranker doesn't keep favouring a brand the
        # filter just proved absent from this topic.
        "metadata_filter": {} if relaxed else metadata_filter,
        "trace": _trace(
            "multi_query_retrieve",
            started,
            queries=per_query,
            candidates=len(candidates),
            quality=round(quality, 3),
            filter=where,
            filter_relaxed=relaxed,
        ),
    }


# --------------------------------------------------------------------------
# 4. Grade retrieval quality -> refine or proceed
# --------------------------------------------------------------------------
def grade_relevance(state: AgentState) -> str:
    candidates = state.get("candidates") or []
    quality = state.get("retrieval_quality", 0.0)
    attempts = state.get("refine_attempts", 0)

    if not candidates and attempts < MAX_REFINE_ATTEMPTS:
        return "refine"
    if quality < GOOD_ENOUGH_SCORE and attempts < MAX_REFINE_ATTEMPTS:
        return "refine"
    if not candidates:
        return "empty"
    return "select"


async def refine_query(state: AgentState) -> dict:
    """Rewrite the queries after a weak retrieval. Bounded by MAX_REFINE_ATTEMPTS."""
    started = time.perf_counter()
    attempts = state.get("refine_attempts", 0) + 1
    queries = state.get("queries") or []
    candidates = state.get("candidates") or []
    quality = state.get("retrieval_quality", 0.0)

    if not settings.gemini_configured:
        # Widen mechanically: shorter queries match more broadly.
        widened = [" ".join(q.split()[:4]) for q in queries if q] or queries
        return {
            "queries": widened,
            "refine_attempts": attempts,
            "metadata_filter": {},  # drop the brand filter to widen the net
            "trace": _trace("refine_query", started, strategy="mechanical", attempt=attempts),
        }

    try:
        data = await gemini_client.chat_json(
            [
                {"role": "system", "content": prompts.REFINE_SYSTEM},
                {
                    "role": "user",
                    "content": prompts.refine_user_prompt(queries, candidates, quality),
                },
            ],
            purpose="agent_refine",
            user_id=state.get("user_id"),
            temperature=0.5,
            max_tokens=900,
            response_schema=prompts.REFINE_SCHEMA,
        )
        new_queries = [q for q in (data.get("queries") or []) if isinstance(q, str) and q.strip()]
    except Exception as exc:  # noqa: BLE001
        logger.warning("refine_query failed: %s", exc)
        new_queries = [" ".join(q.split()[:4]) for q in queries if q]

    return {
        "queries": new_queries[:4] or queries,
        "refine_attempts": attempts,
        "metadata_filter": {},  # relax filtering on retry
        "trace": _trace(
            "refine_query", started, attempt=attempts, new_queries=new_queries[:4]
        ),
    }


# --------------------------------------------------------------------------
# 5. Re-rank and select
# --------------------------------------------------------------------------
def rerank_and_select(state: AgentState) -> dict:
    """Blend semantic score with behavioural fit, novelty and quality.

    Pure arithmetic - no LLM. Vector similarity alone ignores that the user
    has already seen a product, or that it is from a brand they never look at.
    """
    started = time.perf_counter()
    candidates = list(state.get("candidates") or [])
    profile = state.get("profile") or {}
    seen = set(state.get("seen_product_ids") or [])

    affinity = {
        c["category"]: c["score"] for c in profile.get("top_categories", []) if c.get("category")
    }
    max_affinity = max(affinity.values(), default=1.0) or 1.0
    preferred_brand = state.get("preferred_brand") or (
        state.get("metadata_filter", {}) or {}
    ).get("brand")
    typical_price = profile.get("typical_price")

    # Normalise against the candidate set rather than an absolute scale. Cosine
    # similarity ranges differ hugely between embedding providers (the local
    # fallback compresses everything into ~0.08-0.30), so a fixed scale would
    # waste most of the weight range and make the relevance band meaningless.
    max_sem = max((c.get("score", 0.0) for c in candidates), default=0.0) or 1.0
    max_rrf = max((c.get("rrf", 0.0) for c in candidates), default=0.0) or 1.0

    for candidate in candidates:
        sem = candidate.get("score", 0.0) / max_sem
        rrf = candidate.get("rrf", 0.0) / max_rrf

        # Matching several distinct intents is a strong signal.
        multi = min(candidate.get("matched_queries", 1) / 3.0, 1.0)

        cat_fit = affinity.get(candidate.get("category", ""), 0.0) / max_affinity

        # Topical fit: the only thing that can make an item eligible. Weights
        # sum to 1.0 so this is directly comparable across users and providers.
        topical = 0.46 * sem + 0.22 * rrf + 0.10 * multi + 0.22 * cat_fit

        # Hygiene factors MODULATE relevance, they cannot create it. As additive
        # terms they were worth more (0.22) than category affinity itself (0.18),
        # so an unseen well-rated product with no topical connection outscored a
        # perfectly on-topic one - a desk lamp ranked 3rd for someone shopping
        # running shoes. Multiplying keeps them as tie-breakers.
        brand_fit = 1.0 if (preferred_brand and candidate.get("brand") == preferred_brand) else 0.88
        quality = 0.94 + 0.06 * min(candidate.get("rating", 0.0) / 5.0, 1.0)

        # Already-viewed is a mild discount, not a veto: "the one you keep coming
        # back to" is a legitimate recommendation, just weaker than something new.
        novelty = 1.0 if candidate["id"] not in seen else 0.90

        price_fit = 1.0
        if typical_price:
            ratio = abs(candidate.get("price", 0.0) - typical_price) / max(typical_price, 1.0)
            price_fit = 0.96 + 0.04 * max(0.0, 1.0 - min(ratio, 1.0))

        candidate["topical_score"] = topical
        candidate["final_score"] = topical * novelty * brand_fit * quality * price_fit

    candidates.sort(key=lambda c: -c["final_score"])

    # Diversity, but not at the cost of relevance. A strict one-per-category
    # cap drags in unrelated products (a kettle for someone shopping trainers)
    # purely to fill slots. So: cap at 2 per category, and only consider
    # candidates within a relevance band of the best match.
    selected: list[dict] = []
    if candidates:
        best = candidates[0]["final_score"]
        floor = max(best * RELEVANCE_BAND, MIN_RELEVANCE_SCORE)
        relevant = [c for c in candidates if c["final_score"] >= floor]

        per_category: dict[str, int] = {}
        limit = settings.rec_max_items

        for candidate in relevant:
            category = candidate.get("category", "")
            if per_category.get(category, 0) >= MAX_PER_CATEGORY:
                continue
            selected.append(candidate)
            per_category[category] = per_category.get(category, 0) + 1
            if len(selected) >= limit:
                break

        # Still short? Relax the category cap before relaxing relevance.
        if len(selected) < limit:
            for candidate in relevant:
                if candidate in selected:
                    continue
                selected.append(candidate)
                if len(selected) >= limit:
                    break

        # Below-band candidates are only worth showing to avoid an empty-looking
        # slate. Padding to the full count with them buys nothing: the item is
        # visibly off-behaviour, and the narrative ends up hedging about it
        # ("if you choose to step outside your pattern"), which weakens every
        # grounded reason next to it. Three strong picks beat four with filler.
        if len(selected) < MIN_SLATE:
            for candidate in candidates:
                if candidate in selected:
                    continue
                candidate["off_band"] = True
                selected.append(candidate)
                if len(selected) >= MIN_SLATE:
                    break

    return {
        "selected": selected,
        "trace": _trace(
            "rerank_and_select",
            started,
            considered=len(candidates),
            selected=[{"id": s["id"], "title": s["title"], "score": round(s["final_score"], 3)} for s in selected],
        ),
    }


# --------------------------------------------------------------------------
# 6. Generate persuasive copy
# --------------------------------------------------------------------------
async def generate_narrative(state: AgentState) -> dict:
    started = time.perf_counter()
    selected = state.get("selected") or []
    profile = state.get("profile") or {}

    if not selected:
        return {
            "headline": "Nothing to recommend yet",
            "narrative": "Browse a few products and we'll tailor recommendations to you.",
            "item_reasons": {},
            "trace": _trace("generate_narrative", started, skipped="no_selection"),
        }

    def _mechanical() -> dict:
        cats = [c["category"] for c in profile.get("top_categories", [])[:2] if c.get("category")]
        focus = " and ".join(cats) if cats else "your recent activity"
        return {
            "headline": f"More from {cats[0]}" if cats else "Picked for you",
            "narrative": (
                f"Based on {focus}, these picks line up with what you've been browsing. "
                "Each one sits close to the products you spent the most time with."
            ),
            "item_reasons": {
                s["id"]: f"Matches your interest in {s.get('category') or 'this area'}."
                for s in selected
            },
            "used_llm": False,
            "trace": _trace("generate_narrative", started, strategy="mechanical"),
        }

    if not settings.gemini_configured:
        return _mechanical()

    try:
        data = await gemini_client.chat_json(
            [
                {"role": "system", "content": prompts.GENERATE_SYSTEM},
                {
                    "role": "user",
                    "content": prompts.generate_user_prompt(
                        profile,
                        selected,
                        user_name=state.get("user_name", ""),
                        mode=state.get("mode", "dashboard"),
                    ),
                },
            ],
            purpose="agent_generate",
            user_id=state.get("user_id"),
            temperature=0.7,
            max_tokens=1600,
            response_schema=prompts.GENERATE_SCHEMA,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("generate_narrative failed, using mechanical copy: %s", exc)
        result = _mechanical()
        result["trace"] = _trace("generate_narrative", started, error=str(exc)[:200])
        return result

    reasons: dict[int, str] = {}
    for item in data.get("items") or []:
        try:
            reasons[int(item["id"])] = str(item.get("reason", ""))[:400]
        except (KeyError, TypeError, ValueError):
            continue

    return {
        "headline": str(data.get("headline") or "")[:255],
        "narrative": str(data.get("narrative") or "")[:2000],
        "item_reasons": reasons,
        "used_llm": True,
        "trace": _trace(
            "generate_narrative", started, headline=str(data.get("headline") or "")[:80]
        ),
    }


# --------------------------------------------------------------------------
# 7. Grounding guard - the difference between "grounded" and "hoped-for"
# --------------------------------------------------------------------------
async def validate_grounding(state: AgentState) -> dict:
    """Drop anything the model invented.

    Two failure modes are checked:
      * a reason attached to an id that was never retrieved
      * a narrative naming a product title that isn't in the slate
    """
    started = time.perf_counter()
    selected = state.get("selected") or []
    reasons = dict(state.get("item_reasons") or {})
    narrative = state.get("narrative") or ""
    headline = state.get("headline") or ""

    valid_ids = {s["id"] for s in selected}
    hallucinated = [pid for pid in reasons if pid not in valid_ids]
    for pid in hallucinated:
        reasons.pop(pid, None)

    # Any selected product missing a reason gets a grounded default.
    for item in selected:
        if item["id"] not in reasons or not reasons[item["id"]].strip():
            reasons[item["id"]] = (
                f"Aligns with your interest in {item.get('category') or 'this topic'}."
            )

    issues: list[str] = []
    if hallucinated:
        issues.append(f"dropped_{len(hallucinated)}_invented_ids")

    # Cheap title check: a title-cased phrase in quotes that we don't stock.
    selected_titles = {s["title"].lower() for s in selected}
    for quoted in _quoted_phrases(narrative):
        if len(quoted) > 12 and quoted.lower() not in selected_titles:
            issues.append("narrative_cites_unknown_title")
            break

    if not headline.strip():
        headline = "Picked for you"

    return {
        "headline": headline,
        "narrative": narrative,
        "item_reasons": reasons,
        "trace": _trace(
            "validate_grounding",
            started,
            valid=len(valid_ids),
            dropped=len(hallucinated),
            issues=issues or ["none"],
        ),
    }


def _quoted_phrases(text: str) -> list[str]:
    import re

    return re.findall(r'"([^"]{4,80})"', text or "")
