"""Catalog service - the single owner of every product mutation.

Every write goes through here so SQL and the vector store cannot drift.

Design decisions worth knowing:
  * `content_hash` fingerprints exactly the text that gets embedded. An edit
    that changes only price/rating skips re-embedding entirely - a real
    caching win, since embedding is the per-product cost.
  * A vector write failure does NOT fail the admin's request. The SQL row
    commits with vector_state='pending' and `reconcile()` repairs it. A
    flaky embedding provider must not block catalog management.
  * `reconcile()` is the enforcement mechanism: it repairs pending/failed
    rows, deletes orphaned vectors, and re-embeds rows whose stored hash or
    embedding provider no longer matches.
  * A provider switch can change the vector *width*, not just the values, and
    Chroma pins a collection's width at first insert. `reconcile()` therefore
    checks the width before touching anything and drops the collection when it
    has changed - see `_ensure_dimension()`.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.ai import embeddings
from app.models import Product
from app.vector import store

logger = logging.getLogger(__name__)

MUTABLE_FIELDS = (
    "title",
    "description",
    "category",
    "subcategory",
    "brand",
    "price",
    "tags",
    "size",
    "color",
    "rating",
    "is_published",
    "image_url",
    "image_credit",
    "image_credit_url",
)

# Only these schemes reach an `<img src>` or an `href`. Neither `javascript:` nor
# `data:` executes in an image source, so this is not closing a live hole - but
# the image URL is the one product field rendered into markup on every surface
# of the storefront, and the credit URL is the one that becomes a link an actual
# visitor clicks. Pinning both to http(s) costs nothing and keeps it that way.
ALLOWED_URL_SCHEMES = ("http://", "https://")

# Long enough for a CDN URL carrying resize and quality parameters, short enough
# that a paste accident cannot write a novel into the row.
MAX_URL_CHARS = 1000


def safe_url(value: str) -> str:
    """A plain http(s) URL, or "" if it is not one.

    Returns "" rather than raising because of where this sits: a malformed image
    URL should drop that product back to its generated panel, not reject an
    otherwise valid product edit and lose the admin's other changes.
    """
    url = " ".join((value or "").split())
    if not url.lower().startswith(ALLOWED_URL_SCHEMES):
        return ""
    return url[:MAX_URL_CHARS]


def _normalise(data: dict) -> dict:
    """Field cleaning applied identically on the create and the update path.

    Both paths funnel through here rather than through the admin form handler,
    so the seed script and anything programmatic get the same treatment as a
    human filling in the form.
    """
    cleaned = dict(data)
    for field in ("image_url", "image_credit_url"):
        if field in cleaned:
            cleaned[field] = safe_url(cleaned[field])
    if "image_credit" in cleaned:
        cleaned["image_credit"] = " ".join(str(cleaned["image_credit"] or "").split())[:160]
    return cleaned


def brands(db: Session) -> list[str]:
    """Distinct brands stocked in the published catalog.

    This is the *vocabulary* the agent is allowed to filter on. `brand` replaced
    a three-value enum as the retrieval facet, so there is no longer a fixed
    list to validate a model-suggested value against - an invented brand would
    build a perfectly valid Chroma filter that matches nothing. Cheap enough to
    run per generation (one DISTINCT over a small table) and deliberately not
    cached, so a brand added in /admin is filterable immediately.
    """
    return list(
        db.scalars(
            select(Product.brand)
            .where(Product.is_published.is_(True), Product.brand != "")
            .distinct()
            .order_by(Product.brand)
        )
    )


def compute_content_hash(product: Product) -> str:
    return hashlib.sha256(product.embedding_text().encode("utf-8")).hexdigest()


def _vector_metadata(product: Product, content_hash: str) -> dict:
    return {
        "product_id": product.id,
        "title": product.title,
        "category": product.category,
        "subcategory": product.subcategory,
        "brand": product.brand,
        "size": product.size,
        "color": product.color,
        "price": float(product.price or 0.0),
        "rating": float(product.rating or 0.0),
        "tags": product.tags or [],
        "is_published": bool(product.is_published),
        "content_hash": content_hash,
        "embed_provider": embeddings.provider_id(),
    }


async def sync_product_vector(db: Session, product: Product, *, force: bool = False) -> bool:
    """Push one product into the vector store. Returns True when synced.

    Skips the embedding call when the content hash is unchanged and the row is
    already marked synced (unless `force`).
    """
    content_hash = compute_content_hash(product)
    unchanged = (
        not force
        and product.content_hash == content_hash
        and product.vector_state == "synced"
        and product.vector_synced_at is not None
    )
    if unchanged:
        return True

    try:
        vector = await embeddings.embed_texts(
            [product.embedding_text()], purpose="embed_product"
        )
        if not vector or not vector[0]:
            raise RuntimeError("empty embedding returned")

        store.upsert_product(
            product.id,
            document=product.embedding_text(),
            embedding=vector[0],
            metadata=_vector_metadata(product, content_hash),
        )
        product.content_hash = content_hash
        product.vector_state = "synced"
        product.vector_error = ""
        product.vector_synced_at = datetime.now(timezone.utc)
        db.commit()
        return True

    except Exception as exc:
        logger.warning("Vector sync failed for product %s: %s", product.id, exc)
        product.vector_state = "failed"
        product.vector_error = str(exc)[:500]
        db.commit()
        return False


async def create_product(db: Session, data: dict) -> Product:
    data = _normalise(data)
    product = Product(**{k: v for k, v in data.items() if k in MUTABLE_FIELDS})
    db.add(product)
    db.commit()
    db.refresh(product)
    await sync_product_vector(db, product)
    return product


async def update_product(db: Session, product: Product, data: dict) -> Product:
    data = _normalise(data)
    for field in MUTABLE_FIELDS:
        if field in data:
            setattr(product, field, data[field])
    db.commit()
    db.refresh(product)
    await sync_product_vector(db, product)
    return product


def delete_product(db: Session, product: Product) -> None:
    """Delete from both stores. Vector removal first so a failure there
    doesn't leave a live vector pointing at a deleted row."""
    product_id = product.id
    try:
        store.delete_product(product_id)
    except Exception:
        logger.warning("Vector delete failed for product %s", product_id, exc_info=True)
    db.delete(product)
    db.commit()


# --------------------------------------------------------------------------
# Sync enforcement
# --------------------------------------------------------------------------
def _ensure_dimension(db: Session, report: dict) -> None:
    """Drop the collection if its vectors are the wrong width for this process.

    Chroma pins a collection's dimensionality at its first insert. Every later
    upsert *and every query* with a different width raises, so switching
    embedding provider - or just retuning GEMINI_EMBED_DIMENSIONS - leaves a
    collection that cannot be repaired one row at a time: `sync_product_vector`
    would catch the error per product and mark the whole catalog
    vector_state='failed' forever, while retrieval returned nothing.

    So the width is checked before the repair loop, and a mismatch resets the
    collection. Everything is re-embedded immediately afterwards by the loop
    below (every row is missing, so every row is a repair), which is expensive
    but only on the run that follows the change.

    SQL is the source of truth here, so nothing is lost: the vectors are a
    derived index. The rows are marked pending rather than trusted, because
    their stored content_hash no longer corresponds to anything in Chroma.
    """
    expected = embeddings.target_dim()
    actual = store.dimension()
    if actual is None or actual == expected:
        return

    logger.warning(
        "Vector width changed (%s -> %s); resetting the collection and re-embedding",
        actual, expected,
    )
    store.reset()
    db.execute(
        update(Product).values(vector_state="pending", vector_synced_at=None, vector_error="")
    )
    db.commit()
    report["dimension_reset"] = {"from": actual, "to": expected}


async def reconcile(db: Session, *, force: bool = False) -> dict:
    """Repair drift between SQL and the vector store.

    Runs on startup, every few minutes via the scheduler, and on demand from
    /admin/sync. Returns a report rendered on that page.
    """
    report = {
        "checked": 0,
        "repaired": 0,
        "reembedded": 0,
        "orphans_deleted": 0,
        "still_failing": 0,
    }

    _ensure_dimension(db, report)

    products = list(db.scalars(select(Product)))
    report["checked"] = len(products)

    try:
        vector_ids = store.all_ids()
    except Exception:
        logger.warning("Could not list vector ids; skipping orphan sweep", exc_info=True)
        vector_ids = set()

    stored_meta = store.get_metadata([p.id for p in products]) if products else {}
    # Observed, not assumed: comparing stored metadata against an optimistic
    # guess would re-embed the whole catalog on every cold start wherever Gemini
    # embeddings are unavailable.
    current_provider = await embeddings.resolve_provider()
    report["provider"] = current_provider

    for product in products:
        meta = stored_meta.get(product.id) or {}
        expected_hash = compute_content_hash(product)

        missing = product.id not in vector_ids
        stale_hash = meta.get("content_hash") != expected_hash
        stale_provider = meta.get("embed_provider") != current_provider
        broken = product.vector_state != "synced"

        if force or missing or stale_hash or stale_provider or broken:
            ok = await sync_product_vector(db, product, force=True)
            if ok:
                report["repaired"] += 1
                if missing or stale_hash or stale_provider:
                    report["reembedded"] += 1
            else:
                report["still_failing"] += 1

    sql_ids = {p.id for p in products}
    for orphan in vector_ids - sql_ids:
        try:
            store.delete_product(orphan)
            report["orphans_deleted"] += 1
        except Exception:
            logger.warning("Failed deleting orphan vector %s", orphan, exc_info=True)

    return report


def sync_status(db: Session) -> dict:
    """Counts for the admin sync dashboard."""
    products = list(db.scalars(select(Product)))
    try:
        vector_ids = store.all_ids()
    except Exception:
        vector_ids = set()

    sql_ids = {p.id for p in products}
    by_state: dict[str, int] = {}
    for product in products:
        by_state[product.vector_state] = by_state.get(product.vector_state, 0) + 1

    # Report the provider recorded in the vectors themselves. This is a sync
    # function so it cannot probe, and printing the optimistic guess would
    # claim Gemini vectors on a catalog embedded locally.
    stored_meta = store.get_metadata(sorted(sql_ids)) if sql_ids else {}
    vector_providers = sorted(
        {str(m.get("embed_provider", "unknown")) for m in stored_meta.values()}
    )

    return {
        "sql_count": len(products),
        "vector_count": len(vector_ids),
        "by_state": by_state,
        "missing_in_vector": sorted(sql_ids - vector_ids),
        "orphan_vectors": sorted(vector_ids - sql_ids),
        "in_sync": len(sql_ids) == len(vector_ids) and not (sql_ids ^ vector_ids)
        and by_state.get("synced", 0) == len(products),
        # What the stored vectors were actually built with (may be >1 mid-migration).
        "vector_providers": vector_providers,
        "provider": vector_providers[0] if len(vector_providers) == 1 else embeddings.provider_id(),
        "configured_provider": embeddings.configured_provider(),
        "provider_observed": embeddings.provider_observed(),
        "using_fallback": embeddings.using_fallback(),
        # Chroma pins the collection width at first insert, so a mismatch here
        # is the one drift that reconcile cannot repair row by row.
        "vector_dim": store.dimension(),
        "target_dim": embeddings.target_dim(),
    }
