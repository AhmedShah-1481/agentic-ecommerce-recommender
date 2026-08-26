"""Recommendation endpoints.

`GET` is always a cached read. Generation happens in the background behind the
trigger gates, so nothing here can put an LLM call on a request's critical path.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Product, User
from app.security import api_user
from app.services import profile as profile_service
from app.services import recommendations as rec_service
from app.services import signal as signal_service
from app.services import triggers
from app.services.presenters import product_json

router = APIRouter(prefix="/api", tags=["recommendations"])


def _serialise(rec, db: Session) -> dict:
    products = {
        p.id: p
        for p in db.query(Product).filter(Product.id.in_([i.product_id for i in rec.items])).all()
    } if rec.items else {}

    return {
        "id": rec.id,
        "headline": rec.headline,
        "narrative": rec.narrative,
        "detected_interests": rec.detected_interests or [],
        "trigger_reason": rec.trigger_reason,
        "model": rec.model,
        "is_fallback": rec.is_fallback,
        "generated_at": rec.generated_at.isoformat() if rec.generated_at else None,
        "items": [
            {
                "rank": item.rank,
                # The agent's per-item justification. Rendered as a visible
                # annotation on the card, not a tooltip - it is the output of the
                # whole system, so hiding it behind a hover would bury the point.
                "reason": item.reason,
                # The full card shape, from the same presenter the catalog uses,
                # so the storefront renders a recommended product with the very
                # same component as a grid product.
                "product": product_json(products[item.product_id]),
            }
            for item in rec.items
            # Already guaranteed by `validate_grounding` and re-checked in
            # `_persist`; kept here because a product deleted after the
            # recommendation was written would otherwise KeyError on read.
            if item.product_id in products
        ],
    }


@router.get("/recommendations")
async def get_recommendations(
    user: User = Depends(api_user),
    db: Session = Depends(get_db),
):
    """Cached read plus a background refresh check - never awaits the model."""
    rec = triggers.current_recommendation(db, user.id)
    rec_service.schedule_refresh(user.id)

    if rec is None:
        return {"status": "pending", "recommendation": None}
    return {"status": "ok", "recommendation": _serialise(rec, db)}


@router.get("/me/signal")
async def my_signal(
    limit: int = signal_service.DEFAULT_LIMIT,
    user: User = Depends(api_user),
    db: Session = Depends(get_db),
):
    """The signed-in user's own recent tracked events.

    Scoped to `user.id` from the session cookie and never to a client-supplied
    id: this is behavioural history, so one account must not be able to read
    another's. Read-only - it observes the events table, it does not write to
    it, and it deliberately does not touch the profile or the trigger gates.

    Powers the "Your Signal" panel, which polls it so the tracking pipeline is
    visible while you browse instead of only implied by a recommendation.
    """
    return {"events": signal_service.recent(db, user.id, limit)}


@router.post("/recommendations/refresh")
async def refresh_recommendations(
    user: User = Depends(api_user),
    db: Session = Depends(get_db),
):
    """Explicit user-initiated refresh.

    Still runs the gates - a refresh button that bypassed them would be a free
    way for anyone to burn the budget. The gate evaluation is pure SQL and
    arithmetic so it answers immediately; the generation itself (tens of
    seconds on a reasoning model) is dispatched to the background rather than
    held open in the request. The UI polls GET /api/recommendations.
    """
    snapshot = profile_service.build_snapshot(db, user.id)
    decision = triggers.evaluate(db, user.id, snapshot)

    if decision.should_generate:
        rec_service.schedule_refresh(user.id, mode="manual")

    rec = triggers.current_recommendation(db, user.id)
    return {
        "decision": decision.reason,
        "queued": decision.should_generate,
        "recommendation": _serialise(rec, db) if rec else None,
    }
