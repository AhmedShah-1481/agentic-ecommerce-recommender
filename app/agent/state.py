"""Agent state passed between LangGraph nodes."""

from __future__ import annotations

from typing import Annotated, Any, TypedDict


def _merge_trace(existing: list, incoming: list) -> list:
    """Reducer so each node can append its own trace entry."""
    return (existing or []) + (incoming or [])


class AgentState(TypedDict, total=False):
    # -- inputs ------------------------------------------------------------
    user_id: int
    user_name: str
    profile: dict            # ProfileSnapshot.to_prompt_dict()
    seen_product_ids: list[int]
    preferred_brand: str | None
    mode: str               # "dashboard" | "digest"

    # -- analysis ----------------------------------------------------------
    interests: list[str]
    inferred_intent: str
    unmet_needs: list[str]
    queries: list[str]
    metadata_filter: dict[str, Any]

    # -- retrieval ---------------------------------------------------------
    candidates: list[dict]   # [{id,title,category,brand,price,score,...}]
    retrieval_quality: float
    refine_attempts: int
    selected: list[dict]

    # -- generation --------------------------------------------------------
    headline: str
    narrative: str
    item_reasons: dict[int, str]

    # -- bookkeeping -------------------------------------------------------
    trace: Annotated[list[dict], _merge_trace]
    used_llm: bool
    is_fallback: bool
    error: str
