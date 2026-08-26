"""The pages this app still renders server-side.

Everything customer-facing moved to JSON in `storefront.py` and is drawn by the
React app; what is left here is `/inbox`, which exists precisely *because* it is
server-rendered - it serves the real HTML the mailer produced, so the scheduled
digest is demonstrable on a fresh clone with no SMTP credentials. Rebuilding it
in React would mean rendering a copy of the email instead of the email.

`/` is no longer the catalog. It answers with a small index pointing at the
storefront and the admin panel, so hitting the API origin directly tells you
where the site is rather than 404-ing.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models import DigestLog, User
from app.security import current_user
from app.services import mailer
from app.templating import templates

router = APIRouter(tags=["pages"])


@router.get("/", include_in_schema=False)
async def index(request: Request):
    """Signpost, not a storefront.

    A browser that lands on the API origin gets the storefront URL; anything
    reading it as JSON gets the same two links as data. Kept trivial on purpose -
    this is not a page anyone should be building on.
    """
    return {
        "app": settings.app_name,
        "storefront": settings.storefront_url,
        "admin": f"{settings.base_url}/admin",
        "docs": f"{settings.base_url}/docs",
    }


# --------------------------------------------------------------------------
# Inbox - the proactive digests the scheduler sent
# --------------------------------------------------------------------------
@router.get("/inbox", response_class=HTMLResponse)
async def inbox(
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Where scheduled digests land when EMAIL_BACKEND=outbox.

    The point is that the feature is demonstrable on a fresh clone with no SMTP
    credentials: these are the real messages, rendered from the real template.
    """
    history = list(
        db.scalars(
            select(DigestLog)
            .where(DigestLog.user_id == user.id)
            .order_by(DigestLog.created_at.desc())
            .limit(20)
        )
    )
    return templates.TemplateResponse(
        request,
        "inbox.html",
        {
            "title": "Inbox",
            "user": user,
            "messages": mailer.list_outbox(recipient=user.email),
            "history": history,
            "backend": settings.email_backend,
        },
    )


@router.get("/inbox/{name}", response_class=HTMLResponse)
async def inbox_message(name: str, user: User = Depends(current_user)):
    # Scoped to the signed-in address: the outbox directory is shared, so
    # ownership is enforced here rather than assumed from the URL.
    html = mailer.read_outbox(name, recipient=user.email)
    if html is None:
        raise HTTPException(status_code=404, detail="Message not found")
    return HTMLResponse(html)
