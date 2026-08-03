"""SQLAlchemy models.

Eight tables: users, products, events, user_profile_state, recommendations,
recommendation_items, llm_calls, digest_log.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# Users
# --------------------------------------------------------------------------
class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(120), default="")
    role: Mapped[str] = mapped_column(String(20), default="user")  # user | admin
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    events: Mapped[list["Event"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    recommendations: Mapped[list["Recommendation"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    profile_state: Mapped["UserProfileState | None"] = relationship(
        back_populates="user", uselist=False, cascade="all, delete-orphan"
    )

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


# --------------------------------------------------------------------------
# Catalog
# --------------------------------------------------------------------------
class Product(Base):
    """A course. Dual-written to SQL (here) and the vector store.

    `content_hash` fingerprints the embedded text so an edit that doesn't
    change semantics (e.g. price) skips re-embedding entirely.
    `vector_state` lets a failed vector write degrade to a repair job
    instead of failing the admin's request.
    """

    __tablename__ = "products"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(String(80), index=True, default="")
    subcategory: Mapped[str] = mapped_column(String(80), default="")
    level: Mapped[str] = mapped_column(String(30), default="beginner")
    price: Mapped[float] = mapped_column(Float, default=0.0)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    instructor: Mapped[str] = mapped_column(String(120), default="")
    duration_hours: Mapped[float] = mapped_column(Float, default=0.0)
    rating: Mapped[float] = mapped_column(Float, default=0.0)
    is_published: Mapped[bool] = mapped_column(Boolean, default=True)

    content_hash: Mapped[str] = mapped_column(String(64), default="")
    vector_state: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    vector_error: Mapped[str] = mapped_column(Text, default="")
    vector_synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    def embedding_text(self) -> str:
        """The exact text that gets embedded - also what content_hash covers."""
        tags = ", ".join(self.tags or [])
        return (
            f"{self.title}\n"
            f"Category: {self.category} / {self.subcategory}\n"
            f"Level: {self.level}\n"
            f"Tags: {tags}\n"
            f"{self.description}"
        )


# --------------------------------------------------------------------------
# Behavioural events
# --------------------------------------------------------------------------
class Event(Base):
    """One tracked user action.

    `event_id` is generated client-side so a retried flush is idempotent.
    """

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    session_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    event_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    product_id: Mapped[int | None] = mapped_column(
        ForeignKey("products.id", ondelete="SET NULL"), nullable=True, index=True
    )
    search_query: Mapped[str] = mapped_column(String(255), default="")
    path: Mapped[str] = mapped_column(String(255), default="")
    dwell_ms: Mapped[int] = mapped_column(Integer, default=0)
    scroll_pct: Mapped[int] = mapped_column(Integer, default=0)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    client_ts: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    server_ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)

    user: Mapped["User | None"] = relationship(back_populates="events")
    product: Mapped["Product | None"] = relationship()

    __table_args__ = (
        Index("ix_events_user_time", "user_id", "server_ts"),
        Index("ix_events_user_type", "user_id", "event_type"),
    )


class UserProfileState(Base):
    """Cheap counters that gate LLM spend without touching the model."""

    __tablename__ = "user_profile_state"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    events_since_last_rec: Mapped[int] = mapped_column(Integer, default=0)
    last_rec_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_signature: Mapped[str] = mapped_column(String(64), default="")
    generating_since: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    top_categories: Mapped[list] = mapped_column(JSON, default=list)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    user: Mapped["User"] = relationship(back_populates="profile_state")


# --------------------------------------------------------------------------
# Recommendations
# --------------------------------------------------------------------------
class Recommendation(Base):
    __tablename__ = "recommendations"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    headline: Mapped[str] = mapped_column(String(255), default="")
    narrative: Mapped[str] = mapped_column(Text, default="")
    trigger_reason: Mapped[str] = mapped_column(String(120), default="")
    behavior_signature: Mapped[str] = mapped_column(String(64), default="", index=True)
    detected_interests: Mapped[list] = mapped_column(JSON, default=list)
    model: Mapped[str] = mapped_column(String(80), default="")
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    is_fallback: Mapped[bool] = mapped_column(Boolean, default=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    agent_trace: Mapped[dict] = mapped_column(JSON, default=dict)

    user: Mapped["User"] = relationship(back_populates="recommendations")
    items: Mapped[list["RecommendationItem"]] = relationship(
        back_populates="recommendation",
        cascade="all, delete-orphan",
        order_by="RecommendationItem.rank",
    )

    __table_args__ = (Index("ix_rec_user_current", "user_id", "is_current"),)


class RecommendationItem(Base):
    __tablename__ = "recommendation_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    rec_id: Mapped[int] = mapped_column(
        ForeignKey("recommendations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), nullable=False
    )
    rank: Mapped[int] = mapped_column(Integer, default=0)
    reason: Mapped[str] = mapped_column(Text, default="")
    retrieval_score: Mapped[float] = mapped_column(Float, default=0.0)
    final_score: Mapped[float] = mapped_column(Float, default=0.0)

    recommendation: Mapped["Recommendation"] = relationship(back_populates="items")
    product: Mapped["Product"] = relationship()


# --------------------------------------------------------------------------
# Observability
# --------------------------------------------------------------------------
class LLMCall(Base):
    """One row per Mesh call - the receipt behind the efficiency claims."""

    __tablename__ = "llm_calls"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    purpose: Mapped[str] = mapped_column(String(60), default="", index=True)
    model: Mapped[str] = mapped_column(String(80), default="")
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    cache_hit: Mapped[bool] = mapped_column(Boolean, default=False)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class DigestLog(Base):
    """Unique (user, date) makes a double scheduler run a no-op."""

    __tablename__ = "digest_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    digest_date: Mapped[date] = mapped_column(Date, nullable=False)
    rec_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    channel: Mapped[str] = mapped_column(String(30), default="email")
    status: Mapped[str] = mapped_column(String(30), default="sent")
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    user: Mapped["User"] = relationship()

    __table_args__ = (UniqueConstraint("user_id", "digest_date", name="uq_digest_user_date"),)
