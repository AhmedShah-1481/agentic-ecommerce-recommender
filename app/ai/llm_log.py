"""Records every Gemini call so AI spend is auditable at /admin/ai-usage.

Logging must never break a request, so all failures here are swallowed.
"""

from __future__ import annotations

import logging

from app.db import SessionLocal

logger = logging.getLogger(__name__)


def record_call(
    *,
    purpose: str,
    model: str,
    user_id: int | None = None,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    latency_ms: int = 0,
    cache_hit: bool = False,
    ok: bool = True,
    error: str = "",
) -> None:
    from app.models import LLMCall

    db = SessionLocal()
    try:
        db.add(
            LLMCall(
                purpose=purpose,
                model=model,
                user_id=user_id,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                latency_ms=latency_ms,
                cache_hit=cache_hit,
                ok=ok,
                error=error,
            )
        )
        db.commit()
    except Exception:  # never let telemetry break the caller
        logger.debug("llm_call logging failed", exc_info=True)
        db.rollback()
    finally:
        db.close()
