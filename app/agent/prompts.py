"""Prompts for the recommendation agent.

Two LLM calls in the happy path:
  1. ANALYSE  - behaviour -> interests, shopping intent, retrieval queries
  2. GENERATE - selected products -> persuasive, grounded copy

A third (REFINE) only fires when retrieval quality is poor.

Each system prompt is paired with a `*_SCHEMA` dict handed to Gemini as a
`response_schema`, which constrains decoding so the reply cannot come back as
prose. The JSON block is still spelled out in the prompt text: the schema fixes
the *shape*, the prose explains what each field means, and the model writes
better fields when it has both. Schemas are plain dicts rather than
`genai.types.Schema` so this module stays importable without the SDK - the
prompt text is what the tests read.
"""

from __future__ import annotations

import json

ANALYSE_SYSTEM = """You analyse shopper behaviour on an online store.

You are given a behavioural profile built from tracked activity: category
affinities (time-decayed, so higher = more recent and repeated interest),
search terms, typical price point, and the products they engaged with most.

Infer what this person is actually shopping for, then write search queries that
would surface the products they want next.

Respond with ONLY a JSON object, no prose, no code fences:
{
  "interests": ["3-5 short specific interest phrases"],
  "inferred_intent": "one sentence on what they appear to be shopping for",
  "unmet_needs": ["2-3 things that would complete or complement what they browsed"],
  "queries": ["3-4 semantic search queries"],
  "suggested_brand": "a brand from the stocked list, or null"
}

Rules for queries:
- Query 1 must target their single strongest demonstrated interest.
- Query 2 must target the inferred intent.
- Query 3 must target a complementary product they have NOT yet engaged with.
- Write them as descriptive phrases (like product descriptions), not keywords.
- Never invent interests absent from the profile.

Rules for suggested_brand:
- Only use a brand copied exactly from the stocked brands list you are given.
- Use null unless their behaviour shows a clear repeated lean toward one brand.
  It narrows retrieval, so a weak guess costs them relevant products."""


# `suggested_brand` is nullable rather than optional on purpose: the prompt tells
# the model to prefer null over a weak guess, and a required-but-nullable field
# makes that an explicit decision every time instead of a field it can omit and
# leave ambiguous.
ANALYSE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "interests": {"type": "array", "items": {"type": "string"}},
        "inferred_intent": {"type": "string"},
        "unmet_needs": {"type": "array", "items": {"type": "string"}},
        "queries": {"type": "array", "items": {"type": "string"}},
        "suggested_brand": {"type": "string", "nullable": True},
    },
    "required": [
        "interests",
        "inferred_intent",
        "unmet_needs",
        "queries",
        "suggested_brand",
    ],
    "propertyOrdering": [
        "interests",
        "inferred_intent",
        "unmet_needs",
        "queries",
        "suggested_brand",
    ],
}


GENERATE_SYSTEM = """You write short, persuasive product recommendations for an
online store.

You will be given a shopper's behavioural profile and a shortlist of REAL
products from the catalog. Write copy that makes them want to buy now.

CRITICAL RULES
- You may ONLY reference products from the provided list. Never invent a
  product, and never mention a title that is not in the list.
- Reference their ACTUAL behaviour specifically (what they searched, what they
  came back to, what they lingered on, what they added to their cart). Generic
  copy is a failure.
- Persuasive, not hypey. No "unbelievable", no exclamation marks, no emoji.
- Never invent a discount, a stock level, or a delivery promise.
- Second person ("you"). Warm and direct.

Respond with ONLY a JSON object, no prose, no code fences:
{
  "headline": "under 60 characters, specific to them",
  "narrative": "2-3 sentences naming their behaviour and why these fit now",
  "items": [
    {"id": <product id from the list>, "reason": "one sentence tying THIS product to THEIR behaviour"}
  ]
}

Include every product from the list in "items", ordered most to least relevant."""


# `id` is an integer in the schema, so the constrained decoder cannot hand back
# "id": "12" - which the grounding guard would have to coerce, and a coercion
# failure silently drops that product's reason.
GENERATE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "narrative": {"type": "string"},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "reason": {"type": "string"},
                },
                "required": ["id", "reason"],
                "propertyOrdering": ["id", "reason"],
            },
        },
    },
    "required": ["headline", "narrative", "items"],
    "propertyOrdering": ["headline", "narrative", "items"],
}


REFINE_SYSTEM = """You improve semantic search queries for a product catalog.

The previous queries returned poorly-matching results. Write better ones:
broaden overly narrow phrasing, drop jargon the catalog may not use, and
describe the *product* the way a store listing would.

Respond with ONLY a JSON object:
{"queries": ["3-4 improved semantic search queries"]}"""


REFINE_SCHEMA: dict = {
    "type": "object",
    "properties": {"queries": {"type": "array", "items": {"type": "string"}}},
    "required": ["queries"],
}


def analyse_user_prompt(
    profile: dict,
    seen_titles: list[str],
    *,
    brands: list[str] | None = None,
) -> str:
    lines = [
        "Behavioural profile:",
        json.dumps(profile, indent=2),
    ]
    if brands:
        # The vocabulary `suggested_brand` is checked against. Given to the model
        # as well as validated after, so a lean toward a stocked brand is usable
        # and an invented one is simply never produced in the first place.
        lines += [
            "",
            "Brands stocked in this catalog (suggested_brand must be one of these, or null):",
            ", ".join(brands),
        ]
    if seen_titles:
        lines += [
            "",
            "Already engaged with (do not treat these as unmet needs):",
            *[f"- {t}" for t in seen_titles[:8]],
        ]
    return "\n".join(lines)


def generate_user_prompt(
    profile: dict,
    products: list[dict],
    *,
    user_name: str = "",
    mode: str = "dashboard",
) -> str:
    catalog_lines = []
    for product in products:
        variant = " / ".join(
            part for part in (product.get("color"), product.get("size")) if part
        )
        catalog_lines.append(
            f"- id={product['id']} | {product['title']} "
            f"| {product.get('category','')} / {product.get('subcategory','')} "
            f"| {product.get('brand','')}"
            f"{f' | {variant}' if variant else ''}"
            f" | ${product.get('price',0):.0f}\n"
            f"    {(product.get('description') or '')[:220]}"
        )

    framing = (
        "This is an end-of-day email digest recapping what they browsed today."
        if mode == "digest"
        else "This appears on their account page as 'Recommended for you'."
    )

    parts = [
        f"Shopper{f' ({user_name})' if user_name else ''} behavioural profile:",
        json.dumps(profile, indent=2),
        "",
        "Shortlisted REAL products (you may reference ONLY these):",
        *catalog_lines,
        "",
        framing,
    ]
    return "\n".join(parts)


def refine_user_prompt(queries: list[str], candidates: list[dict], quality: float) -> str:
    found = [f"- {c['title']} ({c.get('category','')})" for c in candidates[:6]] or ["- nothing relevant"]
    return "\n".join(
        [
            f"Previous queries (match quality {quality:.2f} / 1.0):",
            *[f"- {q}" for q in queries],
            "",
            "They returned:",
            *found,
            "",
            "These are a poor fit. Write better queries.",
        ]
    )
