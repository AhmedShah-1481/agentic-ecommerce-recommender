"""Event ingestion endpoint.

Returns 202 immediately after handing the batch to an in-memory queue - no
database work happens on the request path.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, status

from app.models import User
from app.schemas import EventBatch
from app.security import optional_user, optional_user_id
from app.services import events as event_service

router = APIRouter(prefix="/api", tags=["events"])


def _payload(batch: EventBatch, uid: int | None) -> list[dict]:
    events = []
    for event in batch.events:
        data = event.model_dump()
        data["user_id"] = uid  # trust the session, never the client
        events.append(data)
    return events


@router.post("/events", status_code=status.HTTP_202_ACCEPTED)
async def ingest_events(
    batch: EventBatch,
    uid: int | None = Depends(optional_user_id),
):
    """Enqueue and return.

    Deliberately no DB session anywhere in this handler - not even to resolve
    the user. This is the busiest endpoint in the app; a connection checkout
    plus a SELECT per flush is exactly the request-path work the queue design
    exists to avoid.
    """
    if not batch.events:
        return {"accepted": 0}
    return {"accepted": event_service.enqueue(_payload(batch, uid))}


@router.post("/events/beacon", status_code=status.HTTP_202_ACCEPTED)
async def ingest_beacon(
    request: Request,
    uid: int | None = Depends(optional_user_id),
):
    """Unload-path variant used by `navigator.sendBeacon`.

    Separate from /events because it must never return a non-2xx: the page is
    already gone, nothing can retry, and a 422 would just be log noise. Beacons
    also arrive as a Blob whose content-type some browsers drop, so the body is
    parsed defensively rather than by the request model.
    """
    try:
        raw = await request.json()
        batch = EventBatch.model_validate(raw)
    except Exception:
        return {"accepted": 0}

    return {"accepted": event_service.enqueue(_payload(batch, uid))}


@router.get("/events/stats")
async def event_stats(user: User | None = Depends(optional_user)):
    """Queue health - surfaced on the admin dashboard."""
    if user is None or not user.is_admin:
        return {"detail": "forbidden"}
    return event_service.stats()
