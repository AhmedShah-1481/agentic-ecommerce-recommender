"""Prompts for the recommendation agent.

Two LLM calls in the happy path:
  1. ANALYSE  - behaviour -> interests, goal, retrieval queries
  2. GENERATE - selected courses -> persuasive, grounded copy

A third (REFINE) only fires when retrieval quality is poor.
"""

from __future__ import annotations

import json

ANALYSE_SYSTEM = """You analyse learner behaviour on an online course platform.

You are given a behavioural profile built from tracked activity: category
affinities (time-decayed, so higher = more recent and repeated interest),
search terms, and the courses they engaged with most.

Infer what this person is actually trying to achieve, then write search
queries that would surface the courses they need next.

Respond with ONLY a JSON object, no prose, no code fences:
{
  "interests": ["3-5 short specific interest phrases"],
  "inferred_goal": "one sentence on what they appear to be working toward",
  "skill_gaps": ["2-3 things they have not covered yet but plausibly need"],
  "queries": ["3-4 semantic search queries"],
  "suggested_level": "beginner" | "intermediate" | "advanced"
}

Rules for queries:
- Query 1 must target their single strongest demonstrated interest.
- Query 2 must target the inferred goal.
- Query 3 must target an adjacent skill they have NOT yet engaged with.
- Write them as descriptive phrases (like course descriptions), not keywords.
- Never invent interests absent from the profile."""


GENERATE_SYSTEM = """You write short, persuasive course recommendations for a
learning platform.

You will be given a learner's behavioural profile and a shortlist of REAL
courses from the catalog. Write copy that makes them want to start now.

CRITICAL RULES
- You may ONLY reference courses from the provided list. Never invent a course,
  and never mention a title that is not in the list.
- Reference their ACTUAL behaviour specifically (what they searched, what they
  returned to, what they lingered on). Generic copy is a failure.
- Persuasive, not hypey. No "unbelievable", no exclamation marks, no emoji.
- Second person ("you"). Warm and direct.

Respond with ONLY a JSON object, no prose, no code fences:
{
  "headline": "under 60 characters, specific to them",
  "narrative": "2-3 sentences naming their behaviour and why these fit now",
  "items": [
    {"id": <course id from the list>, "reason": "one sentence tying THIS course to THEIR behaviour"}
  ]
}

Include every course from the list in "items", ordered most to least relevant."""


REFINE_SYSTEM = """You improve semantic search queries for a course catalog.

The previous queries returned poorly-matching results. Write better ones:
broaden overly narrow phrasing, drop jargon the catalog may not use, and
describe the *topic* the way a course description would.

Respond with ONLY a JSON object:
{"queries": ["3-4 improved semantic search queries"]}"""


def analyse_user_prompt(profile: dict, seen_titles: list[str]) -> str:
    lines = [
        "Behavioural profile:",
        json.dumps(profile, indent=2),
    ]
    if seen_titles:
        lines += [
            "",
            "Already engaged with (do not treat these as gaps):",
            *[f"- {t}" for t in seen_titles[:8]],
        ]
    return "\n".join(lines)


def generate_user_prompt(
    profile: dict,
    courses: list[dict],
    *,
    user_name: str = "",
    mode: str = "dashboard",
) -> str:
    catalog_lines = []
    for course in courses:
        catalog_lines.append(
            f"- id={course['id']} | {course['title']} "
            f"| {course.get('category','')} / {course.get('subcategory','')} "
            f"| {course.get('level','')} | ${course.get('price',0):.0f}\n"
            f"    {(course.get('description') or '')[:220]}"
        )

    framing = (
        "This is an end-of-day email digest recapping what they explored today."
        if mode == "digest"
        else "This appears on their dashboard as 'Recommended for you'."
    )

    parts = [
        f"Learner{f' ({user_name})' if user_name else ''} behavioural profile:",
        json.dumps(profile, indent=2),
        "",
        "Shortlisted REAL courses (you may reference ONLY these):",
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
