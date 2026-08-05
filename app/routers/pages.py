"""Public and signed-in pages."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.config import settings
from app.db import get_db
from app.models import DigestLog, Product, Recommendation, RecommendationItem, User
from app.security import current_user, optional_user
from app.services import mailer
from app.services import recommendations as rec_service
from app.services import signal as signal_service
from app.templating import templates

router = APIRouter(tags=["pages"])

PAGE_SIZE = 12


@router.get("/", response_class=HTMLResponse)
async def catalog(
    request: Request,
    q: str = "",
    category: str = "",
    level: str = "",
    page: int = 1,
    user: User | None = Depends(optional_user),
    db: Session = Depends(get_db),
):
    query = select(Product).where(Product.is_published.is_(True))

    term = (q or "").strip()
    if term:
        like = f"%{term.lower()}%"
        query = query.where(
            or_(
                func.lower(Product.title).like(like),
                func.lower(Product.description).like(like),
                func.lower(Product.category).like(like),
                func.lower(Product.subcategory).like(like),
            )
        )
    if category:
        query = query.where(Product.category == category)
    if level:
        query = query.where(Product.level == level)

    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    page = max(1, page)
    products = list(
        db.scalars(
            query.order_by(Product.rating.desc(), Product.id)
            .offset((page - 1) * PAGE_SIZE)
            .limit(PAGE_SIZE)
        )
    )

    categories = list(
        db.scalars(
            select(Product.category)
            .where(Product.is_published.is_(True), Product.category != "")
            .distinct()
            .order_by(Product.category)
        )
    )

    return templates.TemplateResponse(
        request,
        "catalog.html",
        {
            "title": "Browse courses",
            "user": user,
            "products": products,
            "categories": categories,
            "q": term,
            "category": category,
            "level": level,
            "page": page,
            "total": total,
            "has_next": page * PAGE_SIZE < total,
        },
    )


@router.get("/product/{product_id}", response_class=HTMLResponse)
async def product_detail(
    request: Request,
    product_id: int,
    user: User | None = Depends(optional_user),
    db: Session = Depends(get_db),
):
    product = db.get(Product, product_id)
    if product is None or not product.is_published:
        raise HTTPException(status_code=404, detail="Course not found")

    related = list(
        db.scalars(
            select(Product)
            .where(
                Product.is_published.is_(True),
                Product.category == product.category,
                Product.id != product.id,
            )
            .order_by(Product.rating.desc())
            .limit(3)
        )
    )

    return templates.TemplateResponse(
        request,
        "product.html",
        {
            "title": product.title,
            "user": user,
            "product": product,
            "related": related,
            # Signed-in only: an anonymous visitor has no per-user history to
            # show, and `signal` being falsy hides the panel in the template.
            "signal": signal_service.recent(db, user.id) if user else [],
        },
    )


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Stale-while-revalidate: render whatever is cached immediately, then let
    the trigger gates decide in the background whether to refresh it. The page
    never awaits the model."""
    rec = db.scalars(
        select(Recommendation)
        .where(Recommendation.user_id == user.id, Recommendation.is_current.is_(True))
        # Eager-load items and their products: the template renders a card per
        # item, which would otherwise be a lazy SELECT each.
        .options(
            selectinload(Recommendation.items).selectinload(RecommendationItem.product)
        )
        .order_by(Recommendation.generated_at.desc())
        .limit(1)
    ).first()

    rec_service.schedule_refresh(user.id)

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "title": "Your dashboard",
            "user": user,
            "rec": rec,
            # Read-only view of this user's own events. Rendered here so the
            # panel is populated on first paint; ui.js then polls to keep it
            # current without a reload.
            "signal": signal_service.recent(db, user.id),
        },
    )


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
