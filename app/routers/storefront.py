"""Storefront JSON API - the catalog the React app reads.

These three endpoints replace the HTML-rendering `/`, `/product/{id}` and
`/dashboard` routes that used to live in `pages.py`. `/admin/*` stays on Jinja
(internal tooling), and so does `/inbox`, which renders the real digest HTML the
mailer produced.

All three are public: browsing must work signed out, and the header asks
`/api/auth/me` separately for its own state. Nothing here depends on
`optional_user`, which keeps a connection checkout and a SELECT off the busiest
read path in the app for a value it would not use.

There is deliberately no `/api/dashboard`. The "For you" page needs a cached
recommendation and the user's recent signal, and `GET /api/recommendations` and
`GET /api/me/signal` in `recs.py` already are exactly those two things - both
already scoped to the session cookie, both already behind `api_user`. A third
endpoint would only re-serialise what they return and give the two copies a
chance to drift.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Product
from app.services.presenters import product_json

router = APIRouter(prefix="/api", tags=["storefront"])

PAGE_SIZE = 12
MAX_PAGE_SIZE = 48
RELATED_COUNT = 4

# Sort keys the client may send, mapped to an ORDER BY. Whitelisted rather than
# interpolated: a sort parameter that reaches the query builder as a column name
# is an injection surface, and an unknown key should fall back to the default
# rather than 500.
SORTS: dict[str, tuple] = {
    "featured": (Product.rating.desc(), Product.id.asc()),
    "price_asc": (Product.price.asc(), Product.id.asc()),
    "price_desc": (Product.price.desc(), Product.id.asc()),
    "rating": (Product.rating.desc(), Product.price.asc()),
    "newest": (Product.created_at.desc(), Product.id.desc()),
}
DEFAULT_SORT = "featured"


def _published() -> Select:
    return select(Product).where(Product.is_published.is_(True))


def _apply_filters(
    query: Select,
    *,
    q: str,
    category: str,
    brand: str,
    min_price: float | None,
    max_price: float | None,
    min_rating: float | None,
) -> Select:
    """Narrow a product query by the storefront's filter set.

    Split out from the endpoint so the same predicate list builds both the page
    of rows and the total count - two hand-maintained WHERE clauses would
    eventually disagree and paginate against the wrong total.
    """
    term = (q or "").strip()
    if term:
        like = f"%{term.lower()}%"
        query = query.where(
            or_(
                func.lower(Product.title).like(like),
                func.lower(Product.description).like(like),
                func.lower(Product.category).like(like),
                func.lower(Product.subcategory).like(like),
                func.lower(Product.brand).like(like),
                func.lower(Product.color).like(like),
            )
        )
    if category:
        query = query.where(Product.category == category)
    if brand:
        query = query.where(Product.brand == brand)
    if min_price is not None:
        query = query.where(Product.price >= min_price)
    if max_price is not None:
        query = query.where(Product.price <= max_price)
    if min_rating is not None:
        query = query.where(Product.rating >= min_rating)
    return query


@router.get("/catalog")
async def catalog(
    q: str = "",
    category: str = "",
    brand: str = "",
    min_price: float | None = None,
    max_price: float | None = None,
    min_rating: float | None = None,
    sort: str = DEFAULT_SORT,
    page: int = 1,
    page_size: int = PAGE_SIZE,
    db: Session = Depends(get_db),
):
    """A page of published products, filtered and sorted.

    Returns the resolved `sort`, `page` and `page_size` rather than echoing what
    was asked for: all three are clamped, and a client that renders its controls
    from the response stays in step with what the server actually did.
    """
    sort_key = sort if sort in SORTS else DEFAULT_SORT
    page = max(1, page)
    page_size = max(1, min(page_size, MAX_PAGE_SIZE))

    filtered = _apply_filters(
        _published(),
        q=q,
        category=category,
        brand=brand,
        min_price=min_price,
        max_price=max_price,
        min_rating=min_rating,
    )

    total = db.scalar(select(func.count()).select_from(filtered.subquery())) or 0
    products = list(
        db.scalars(
            filtered.order_by(*SORTS[sort_key]).offset((page - 1) * page_size).limit(page_size)
        )
    )

    return {
        "products": [product_json(p) for p in products],
        "total": total,
        "page": page,
        "page_size": page_size,
        # Ceiling division. Reported so the client's pagination doesn't have to
        # re-derive it and round differently on an exact multiple.
        "pages": (total + page_size - 1) // page_size if total else 0,
        "has_next": page * page_size < total,
        "has_prev": page > 1,
        "sort": sort_key,
        "q": (q or "").strip(),
    }


@router.get("/catalog/facets")
async def facets(db: Session = Depends(get_db)):
    """Filter-sidebar vocabulary: categories, brands, price and rating bounds.

    Counts are over the whole published catalog and deliberately ignore the
    caller's active filters. Filter-aware counts need a separate aggregate per
    facet per request, and the honest version of that ("Apparel (0)" once you
    pick Footwear) is worse UX than a stable count for a 56-product catalog.

    Bounds come from the data rather than from constants, so the price slider
    ends where the catalog does instead of at a number someone typed once.
    """
    published = Product.is_published.is_(True)

    def _counts(column):
        rows = db.execute(
            select(column, func.count())
            .where(published, column != "")
            .group_by(column)
            .order_by(column)
        ).all()
        return [{"value": value, "count": count} for value, count in rows]

    price_min, price_max = db.execute(
        select(func.min(Product.price), func.max(Product.price)).where(published)
    ).one()

    return {
        "categories": _counts(Product.category),
        "brands": _counts(Product.brand),
        "price": {
            # An empty catalog yields (None, None); 0.0 keeps the client's slider
            # arithmetic from meeting a null.
            "min": float(price_min or 0.0),
            "max": float(price_max or 0.0),
        },
        "sorts": list(SORTS),
        "total": db.scalar(select(func.count(Product.id)).where(published)) or 0,
    }


@router.get("/products/{product_id}")
async def product_detail(product_id: int, db: Session = Depends(get_db)):
    """One product plus a related strip.

    An unpublished product 404s rather than 403s: whether a given id exists but
    is hidden is not something an anonymous caller should be able to probe.
    """
    product = db.get(Product, product_id)
    if product is None or not product.is_published:
        raise HTTPException(status_code=404, detail="Product not found")

    related = list(
        db.scalars(
            _published()
            .where(Product.category == product.category, Product.id != product.id)
            .order_by(Product.rating.desc(), Product.id)
            .limit(RELATED_COUNT)
        )
    )

    return {
        "product": product_json(product, full=True),
        "related": [product_json(p) for p in related],
    }
