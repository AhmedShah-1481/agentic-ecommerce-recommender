"""Behavioural profile aggregation.

Pure Python over the events table - NO LLM involved. This is what makes the
system affordable: the expensive model call only ever sees a compact snapshot,
and the decision of *whether* to call the model is made from these numbers.

Core idea: time-decayed affinity. Every event contributes
    weight * 0.5 ** (age_hours / HALF_LIFE_HOURS)
so this morning's interest outranks last week's, which is what makes the
recommendations feel like they are tracking the user rather than averaging them.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Event, Product

HALF_LIFE_HOURS = 24.0
LOOKBACK_DAYS = 14
MAX_EVENTS = 400

EVENT_WEIGHTS = {
    "search": 3.0,
    "product_view": 1.0,
    "product_click": 2.0,
    "add_to_cart": 6.0,
    "dwell": 0.0,  # scored separately - depends on duration
    "page_view": 0.2,
    "scroll_depth": 0.3,
}

LONG_DWELL_MS = 30_000
DEEP_DWELL_MS = 60_000


@dataclass
class ProfileSnapshot:
    """Compact behavioural summary - the agent's entire view of the user."""

    user_id: int
    total_events: int = 0
    category_affinity: dict[str, float] = field(default_factory=dict)
    subcategory_affinity: dict[str, float] = field(default_factory=dict)
    tag_affinity: dict[str, float] = field(default_factory=dict)
    brand_affinity: dict[str, float] = field(default_factory=dict)
    recent_searches: list[str] = field(default_factory=list)
    viewed_product_ids: list[int] = field(default_factory=list)
    engaged_products: list[dict] = field(default_factory=list)
    price_points: list[float] = field(default_factory=list)
    high_signal: bool = False
    signature: str = ""

    @property
    def top_categories(self) -> list[str]:
        return [c for c, _ in sorted(self.category_affinity.items(), key=lambda kv: -kv[1])[:5]]

    @property
    def top_tags(self) -> list[str]:
        return [t for t, _ in sorted(self.tag_affinity.items(), key=lambda kv: -kv[1])[:8]]

    @property
    def preferred_brand(self) -> str | None:
        if not self.brand_affinity:
            return None
        return max(self.brand_affinity.items(), key=lambda kv: kv[1])[0]

    @property
    def avg_price(self) -> float | None:
        if not self.price_points:
            return None
        return sum(self.price_points) / len(self.price_points)

    def has_signal(self, minimum: int) -> bool:
        return self.total_events >= minimum and bool(self.category_affinity)

    def to_prompt_dict(self) -> dict:
        """What actually gets serialised into the prompt - deliberately small."""
        return {
            "top_categories": [
                {"category": c, "score": round(s, 2)}
                for c, s in sorted(self.category_affinity.items(), key=lambda kv: -kv[1])[:5]
            ],
            "top_tags": self.top_tags,
            "preferred_brand": self.preferred_brand,
            "recent_searches": self.recent_searches[:6],
            "most_engaged": self.engaged_products[:5],
            "typical_price": round(self.avg_price, 2) if self.avg_price else None,
            "total_tracked_events": self.total_events,
        }


def _decay(age_hours: float) -> float:
    return 0.5 ** (age_hours / HALF_LIFE_HOURS)


def _dwell_weight(dwell_ms: int) -> float:
    if dwell_ms >= DEEP_DWELL_MS:
        return 5.0
    if dwell_ms >= LONG_DWELL_MS:
        return 4.0
    if dwell_ms >= 10_000:
        return 1.5
    return 0.3


def build_snapshot(db: Session, user_id: int, *, since_days: int = LOOKBACK_DAYS) -> ProfileSnapshot:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    cutoff = now - timedelta(days=since_days)

    events = list(
        db.scalars(
            select(Event)
            .where(Event.user_id == user_id, Event.server_ts >= cutoff)
            .order_by(Event.server_ts.desc())
            .limit(MAX_EVENTS)
        )
    )

    snapshot = ProfileSnapshot(user_id=user_id, total_events=len(events))
    if not events:
        return snapshot

    product_ids = {e.product_id for e in events if e.product_id}
    products: dict[int, Product] = {}
    if product_ids:
        products = {
            p.id: p for p in db.scalars(select(Product).where(Product.id.in_(product_ids)))
        }

    engagement: dict[int, float] = {}
    seen_searches: set[str] = set()

    for event in events:
        age_hours = max(0.0, (now - event.server_ts).total_seconds() / 3600.0)
        decay = _decay(age_hours)

        if event.event_type == "dwell":
            weight = _dwell_weight(event.dwell_ms)
            if event.dwell_ms >= LONG_DWELL_MS:
                snapshot.high_signal = True
        else:
            weight = EVENT_WEIGHTS.get(event.event_type, 0.5)

        score = weight * decay
        if score <= 0:
            continue

        if event.event_type == "add_to_cart":
            snapshot.high_signal = True

        # Searches carry intent even without a product attached.
        if event.search_query:
            query = event.search_query.strip().lower()
            if query and query not in seen_searches:
                seen_searches.add(query)
                snapshot.recent_searches.append(event.search_query.strip())
            for token in query.split():
                if len(token) > 2:
                    snapshot.tag_affinity[token] = snapshot.tag_affinity.get(token, 0.0) + score * 0.5

        product = products.get(event.product_id) if event.product_id else None
        if product is None:
            continue

        if product.id not in snapshot.viewed_product_ids:
            snapshot.viewed_product_ids.append(product.id)

        engagement[product.id] = engagement.get(product.id, 0.0) + score

        if product.category:
            snapshot.category_affinity[product.category] = (
                snapshot.category_affinity.get(product.category, 0.0) + score
            )
        if product.subcategory:
            snapshot.subcategory_affinity[product.subcategory] = (
                snapshot.subcategory_affinity.get(product.subcategory, 0.0) + score
            )
        if product.brand:
            snapshot.brand_affinity[product.brand] = (
                snapshot.brand_affinity.get(product.brand, 0.0) + score
            )
        for tag in product.tags or []:
            key = str(tag).lower()
            snapshot.tag_affinity[key] = snapshot.tag_affinity.get(key, 0.0) + score * 0.7
        if product.price:
            snapshot.price_points.append(float(product.price))

    # Repeat searching in one area is a strong intent signal.
    if len(snapshot.recent_searches) >= 2:
        snapshot.high_signal = True

    ranked = sorted(engagement.items(), key=lambda kv: -kv[1])
    snapshot.engaged_products = [
        {
            "id": pid,
            "title": products[pid].title,
            "category": products[pid].category,
            "score": round(score, 2),
        }
        for pid, score in ranked[:8]
        if pid in products
    ]

    snapshot.signature = compute_signature(snapshot)
    return snapshot


def compute_signature(snapshot: ProfileSnapshot) -> str:
    """Fingerprint of what the user is currently interested in.

    If this is unchanged since the last recommendation, regenerating would
    produce the same output - so we serve the cache and skip the LLM entirely.
    """
    parts = [
        "|".join(snapshot.top_categories[:5]),
        "|".join(sorted(s.lower() for s in snapshot.recent_searches[:3])),
        "|".join(str(p["id"]) for p in snapshot.engaged_products[:5]),
        snapshot.preferred_brand or "",
    ]
    return hashlib.sha256("::".join(parts).encode()).hexdigest()[:32]
