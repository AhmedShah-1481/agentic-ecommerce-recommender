"""ORM -> JSON for the storefront.

Display layer only, in the same spirit as `signal.py`: nothing here reads the
profile, moves a trigger gate or touches the vector store. It exists so the
catalog router and the recommendations router describe a product *identically* -
the React app renders the same card component for a grid item and for a
recommended item, so two hand-rolled dicts would eventually disagree about a
field name and break one of them.

`password_hash`-style accidents are the reason this builds dicts field by field
rather than serialising the ORM object: a column added to `Product` later joins
the API response only if someone puts it here on purpose.
"""

from __future__ import annotations

from app.models import Product

# Roughly one card's worth of prose. The seed descriptions are 300-500 chars of
# genuinely useful material detail, which is right on a detail page and far too
# much under a grid thumbnail.
BLURB_CHARS = 150


def blurb(description: str, limit: int = BLURB_CHARS) -> str:
    """First sentence of a description, or a clean truncation of it.

    Cuts on a sentence boundary when there is one inside the budget, because a
    complete short sentence reads better on a card than a longer fragment with
    an ellipsis.
    """
    text = " ".join((description or "").split())
    if not text:
        return ""

    stop = text.find(". ")
    if 0 < stop <= limit:
        return text[: stop + 1]

    if len(text) <= limit:
        return text

    cut = text.rfind(" ", 0, limit)
    return text[: cut if cut > 0 else limit].rstrip(",;: ") + "…"


def product_json(product: Product, *, full: bool = False) -> dict:
    """One product as the storefront sees it.

    `full=False` is the grid/card shape: everything needed to render a card -
    its photograph, or the generated panel that stands in when there isn't one -
    with prose trimmed to a blurb. `full=True` adds the complete description for
    the detail page, and the photo credit that page is the only one to display.

    `size` and `color` are included in both, even though `embedding_text()`
    deliberately excludes `size` from what gets embedded. Retrieval and display
    want different things: "M" is noise to a similarity search and is exactly
    what a shopper checks before adding to a cart.
    """
    data = {
        "id": product.id,
        "title": product.title,
        "category": product.category,
        "subcategory": product.subcategory,
        "brand": product.brand,
        "price": float(product.price or 0.0),
        "rating": float(product.rating or 0.0),
        "tags": list(product.tags or []),
        "size": product.size,
        "color": product.color,
        "blurb": blurb(product.description),
        # Always present, "" when the product has no photograph, so the React
        # component tests one shape rather than distinguishing absent from empty.
        "image_url": product.image_url or "",
    }
    if full:
        data["description"] = product.description or ""
        # Attribution rides with the detail page only, because that is the only
        # surface that renders it. A grid of twelve cards would otherwise carry
        # twenty-four strings nothing displays.
        data["image_credit"] = product.image_credit or ""
        data["image_credit_url"] = product.image_credit_url or ""
    return data
