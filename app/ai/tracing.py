"""LangSmith wiring.

Two things live here, both env-gated:

  * `configure_tracing()` translates our own settings into the environment
    variables LangGraph and the LangSmith SDK read. Importing this module runs
    it, so anything imported afterwards sees a configured tracer.
  * `traceable()` is a decorator that adds a LangSmith span when tracing is on
    and returns the function untouched when it is not. That is what lets the
    Mesh client be instrumented without making LangSmith a hard dependency of
    the recommendation path - no key means no spans, not a broken run.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable

from app.config import settings

logger = logging.getLogger(__name__)

_ENV_KEYS = ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2")


def configure_tracing() -> bool:
    """Enable LangSmith if both the flag and a key are present."""
    if not (settings.langsmith_tracing and settings.langsmith_api_key):
        # Clear rather than ignore: a stale env var from the parent shell would
        # otherwise make the SDK try to ship traces to nowhere on every call.
        for key in _ENV_KEYS:
            os.environ.pop(key, None)
        return False

    os.environ["LANGSMITH_TRACING"] = "true"
    os.environ["LANGCHAIN_TRACING_V2"] = "true"
    os.environ["LANGSMITH_API_KEY"] = settings.langsmith_api_key
    os.environ["LANGCHAIN_API_KEY"] = settings.langsmith_api_key
    os.environ["LANGSMITH_PROJECT"] = settings.langsmith_project
    os.environ["LANGCHAIN_PROJECT"] = settings.langsmith_project
    logger.info("LangSmith tracing enabled (project=%s)", settings.langsmith_project)
    return True


TRACING_ENABLED = configure_tracing()


def traceable(**options) -> Callable:
    """`langsmith.traceable`, or a pass-through when tracing is off/absent."""

    def decorator(fn: Callable) -> Callable:
        if not TRACING_ENABLED:
            return fn
        try:
            from langsmith import traceable as ls_traceable
        except ImportError:
            logger.info("LangSmith tracing requested but langsmith is not installed")
            return fn
        return ls_traceable(**options)(fn)

    return decorator


def redact_inputs(inputs: dict) -> dict:
    """Span input formatter for the Mesh client.

    Drops `self` (the client object is noise, and serialising it would carry the
    API key into the trace payload) and keeps the messages, which are the part
    worth inspecting.
    """
    safe = {k: v for k, v in inputs.items() if k != "self"}
    messages = safe.get("messages")
    if isinstance(messages, list):
        safe["messages"] = [
            {"role": m.get("role", ""), "content": str(m.get("content", ""))[:4000]}
            for m in messages
            if isinstance(m, dict)
        ]
    if isinstance(safe.get("texts"), list):
        safe["texts"] = {"count": len(safe["texts"])}
    return safe
