"""Shared Jinja2 environment.

Lives outside the routers so every module renders through the same env with
the same globals and filters.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi.templating import Jinja2Templates

from app.config import settings

templates = Jinja2Templates(directory=str(settings.resolve("app/templates")))


def timeago(value: datetime | None) -> str:
    """Relative time, e.g. "3m ago".

    Public because the signal feed serialises the same wording into JSON, and
    two implementations would drift.
    """
    if not value:
        return "never"
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    reference = value.replace(tzinfo=None) if value.tzinfo else value
    seconds = (now - reference).total_seconds()
    if seconds < 0:
        return "just now"
    if seconds < 60:
        return f"{int(seconds)}s ago"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


def _money(value: float | None) -> str:
    return "Free" if not value else f"${value:,.0f}"


templates.env.filters["timeago"] = timeago
templates.env.filters["money"] = _money
templates.env.globals["app_name"] = settings.app_name
