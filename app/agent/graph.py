"""LangGraph wiring for the recommendation agent.

The graph is compiled once at import and reused. LangSmith tracing, when
configured, picks this up automatically - see app/ai/tracing.py, which sets the
environment variables LangGraph reads.
"""

from __future__ import annotations

import logging

from langgraph.graph import END, StateGraph

from app.agent import nodes
from app.agent.state import AgentState
from app.ai.tracing import TRACING_ENABLED

logger = logging.getLogger(__name__)


def build_graph():
    graph = StateGraph(AgentState)

    graph.add_node("analyze_behavior", nodes.analyze_behavior)
    graph.add_node("cold_start", nodes.cold_start)
    graph.add_node("multi_query_retrieve", nodes.multi_query_retrieve)
    graph.add_node("refine_query", nodes.refine_query)
    graph.add_node("rerank_and_select", nodes.rerank_and_select)
    graph.add_node("generate_narrative", nodes.generate_narrative)
    graph.add_node("validate_grounding", nodes.validate_grounding)

    graph.set_entry_point("analyze_behavior")

    # Real branch: users with no usable behaviour never reach the LLM path.
    graph.add_conditional_edges(
        "analyze_behavior",
        nodes.decide_retrieval,
        {"retrieve": "multi_query_retrieve", "skip": "cold_start"},
    )
    graph.add_edge("cold_start", END)

    # Retrieval quality gate -> refine loop (bounded) or proceed.
    graph.add_conditional_edges(
        "multi_query_retrieve",
        nodes.grade_relevance,
        {
            "refine": "refine_query",
            "select": "rerank_and_select",
            "empty": "cold_start",
        },
    )
    graph.add_edge("refine_query", "multi_query_retrieve")

    graph.add_edge("rerank_and_select", "generate_narrative")
    graph.add_edge("generate_narrative", "validate_grounding")
    graph.add_edge("validate_grounding", END)

    return graph.compile()


recommendation_graph = build_graph()
