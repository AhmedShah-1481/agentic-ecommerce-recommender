"""Pydantic schemas for API payloads."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator

MAX_EVENTS_PER_BATCH = 100


class IncomingEvent(BaseModel):
    """One tracked action from the browser.

    Validation is permissive on purpose: a malformed field should not cause
    the whole batch to 422 and make the client retry forever.
    """

    event_id: str = Field(min_length=1, max_length=64)
    event_type: str = Field(min_length=1, max_length=40)
    session_id: str = Field(default="", max_length=64)
    path: str = Field(default="", max_length=255)
    product_id: int | None = None
    search_query: str = Field(default="", max_length=255)
    dwell_ms: int = 0
    scroll_pct: int = 0
    meta: dict[str, Any] = Field(default_factory=dict)
    client_ts: str | None = None

    @field_validator("dwell_ms")
    @classmethod
    def _clamp_dwell(cls, v: int) -> int:
        # A tab left open overnight shouldn't register as 8h of engagement.
        return max(0, min(int(v or 0), 2 * 60 * 60 * 1000))

    @field_validator("scroll_pct")
    @classmethod
    def _clamp_scroll(cls, v: int) -> int:
        return max(0, min(int(v or 0), 100))

    @field_validator("meta")
    @classmethod
    def _bound_meta(cls, v: dict) -> dict:
        if not isinstance(v, dict):
            return {}
        return dict(list(v.items())[:10])


class EventBatch(BaseModel):
    events: list[IncomingEvent] = Field(default_factory=list, max_length=MAX_EVENTS_PER_BATCH)


class ProductIn(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    description: str = ""
    category: str = Field(default="", max_length=80)
    subcategory: str = Field(default="", max_length=80)
    brand: str = Field(default="", max_length=60)
    price: float = 0.0
    tags: list[str] = Field(default_factory=list)
    size: str = Field(default="One Size", max_length=20)
    color: str = Field(default="", max_length=30)
    rating: float = 0.0
    is_published: bool = True


# --------------------------------------------------------------------------
# JSON auth
#
# Deliberately loose where the form handlers are loose. The real rules live in
# `routers/auth.py` so the HTML and JSON paths enforce one set; duplicating a
# length check here would let the two drift and quietly disagree about what a
# valid password is. `max_length` is only here to bound what reaches bcrypt and
# the database.
# --------------------------------------------------------------------------
class LoginIn(BaseModel):
    email: str = Field(max_length=255)
    password: str = Field(max_length=255)


class RegisterIn(LoginIn):
    name: str = Field(default="", max_length=120)


class PublicUser(BaseModel):
    """What the client is allowed to know about the signed-in account.

    Built field by field rather than from the ORM object, so adding a column to
    `User` can never widen this response by accident - `password_hash` being the
    one that matters.
    """

    id: int
    email: str
    name: str
    role: str
    is_admin: bool
