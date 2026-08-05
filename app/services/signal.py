"""Recent-activity feed for the "Your Signal" panel.

A read-only view over `events` for one user. This is display layer and nothing
else: it feeds no profile, no trigger gate and no prompt. It exists because the
tracking that drives all three is otherwise invisible - the system looks like it
is doing nothing until a recommendation appears minutes later.

The weights shown next to each chip are the *same* constants the profile scores
with (`profile.EVENT_WEIGHTS`), imported rather than restated, so the panel
cannot drift into claiming a weighting the recommender doesn't actually use.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import Event
from app.services.profile import DEEP_DWELL_MS, LONG_DWELL_MS, EVENT_WEIGHTS
from app.templating import timeago

DEFAULT_LIMIT = 8
MAX_LIMIT = 25

# `page_view` is excluded deliberately. It carries no product, scores 0.2, and
# fires on every navigation - including the dashboard the panel lives on, which
# would push real engagement out of an 8-row window within a few reloads.
HIDDEN_TYPES = {"page_view"}

VERBS = {
    "product_view": "Viewed",
    "product_click": "Clicked",
    "search": "Searched",
    "add_to_cart": "Added to cart",
    "dwell": "Dwell",
    "scroll_depth": "Scrolled",
}


def _duration(ms: int) -> str:
    if ms >= 60_000:
        seconds = (ms % 60_000) // 1000
        return f"{ms // 60_000}m{f' {seconds}s' if seconds else ''}"
    return f"{max(1, round(ms / 1000))}s"


def describe(event: Event) -> dict:
    """Turn one row into a chip.

    Pure and side-effect free so it can be unit-tested without a database.
    """
    title = event.product.title if event.product else ""
    verb = VERBS.get(event.event_type, event.event_type.replace("_", " "))
    weight = EVENT_WEIGHTS.get(event.event_type, 0.5)

    # A catalog facet is emitted as a `search` so it reaches the momentum gate
    # and the agent's queries without a new event type. It is still not a typed
    # query, and the panel should not claim the user typed one.
    if event.event_type == "search" and (event.meta or {}).get("source") == "filter":
        verb = "Filtered"

    if event.event_type == "dwell":
        detail = f"{_duration(event.dwell_ms)} on {title}" if title else _duration(event.dwell_ms)
    elif event.event_type == "search":
        detail = event.search_query or ""
    elif event.event_type == "scroll_depth":
        detail = f"{event.scroll_pct}% of {title}" if title else f"{event.scroll_pct}%"
    else:
        detail = title or event.path or ""

    # "Strong" mirrors what actually unblocks the trigger gates: an add to cart,
    # or a dwell past the long threshold. Both set `high_signal` in profile.py.
    strong = event.event_type == "add_to_cart" or (
        event.event_type == "dwell" and event.dwell_ms >= LONG_DWELL_MS
    )

    return {
        "id": event.id,
        "type": event.event_type,
        "verb": verb,
        "detail": detail,
        "product_id": event.product_id,
        "when": timeago(event.server_ts),
        "weight": weight,
        "strong": strong,
        "deep": event.event_type == "dwell" and event.dwell_ms >= DEEP_DWELL_MS,
    }


def recent(db: Session, user_id: int, limit: int = DEFAULT_LIMIT) -> list[dict]:
    """The user's own last `limit` meaningful events, newest first."""
    limit = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
    rows = db.scalars(
        select(Event)
        # One extra query for the products instead of one per row: this runs on
        # a poll, so an N+1 here would be N+1 every few seconds per open tab.
        .options(selectinload(Event.product))
        .where(Event.user_id == user_id, Event.event_type.notin_(HIDDEN_TYPES))
        .order_by(Event.id.desc())
        .limit(limit)
    )
    return [describe(event) for event in rows]
