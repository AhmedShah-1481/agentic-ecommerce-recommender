"""Product imagery: the column, the fallback contract, and the seed rewrite.

Five things here are load-bearing and none of them are visible by reading the
happy path:

  * **A photo swap must not re-embed.** The image columns sit outside
    `embedding_text()`, so `content_hash` cannot move when a photograph changes.
    If that ever regresses, re-shooting the catalog silently bills a full
    re-embed of every product.

  * **The additive migration is idempotent.** A database created before these
    columns existed has to gain them on the next `init_db()`, and a database that
    already has them must not be touched. `create_all` does neither.

  * **A bad URL degrades, it does not raise.** An unusable image URL drops that
    product to its generated panel; it must never reject the rest of an
    otherwise-valid product edit.

  * **Credit does not leak into the grid payload.** Attribution is only rendered
    on the detail page, so only `full=True` carries it.

  * **The seed rewriter round-trips byte for byte.** `scripts/fetch_images.py`
    rewrites `seed/products.json` in place. If its formatter does not reproduce
    the existing style exactly, a three-field change becomes an eleven-hundred
    line diff and nobody can review it.

No network: nothing here calls Unsplash. The fetch script is exercised through
its pure functions only, which is the whole reason those are pure.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import ADDITIVE_COLUMNS, Base, _apply_additive_columns
from app.models import Product
from app.services import catalog
from app.services.presenters import product_json

BASE_DIR = Path(__file__).resolve().parent.parent
SEED_FILE = BASE_DIR / "seed" / "products.json"


def _load_fetch_script():
    """Import scripts/fetch_images.py by path - `scripts/` is not a package."""
    spec = importlib.util.spec_from_file_location(
        "fetch_images", BASE_DIR / "scripts" / "fetch_images.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    yield session
    session.close()


def make_product(**overrides) -> Product:
    fields = dict(
        title="Merino Crew Sweater",
        description="Extra-fine merino, warm without bulk. Holds its shape.",
        category="Apparel",
        subcategory="Knitwear",
        brand="Sundermere",
        price=148.0,
        rating=4.7,
        tags=["merino", "wool"],
        size="M",
        color="Oatmeal",
    )
    fields.update(overrides)
    return Product(**fields)


# --------------------------------------------------------------------------
# The embedding boundary
# --------------------------------------------------------------------------
def test_image_columns_are_outside_the_embedded_text():
    """A photograph is not semantic content."""
    without = make_product()
    with_photo = make_product(
        image_url="https://images.unsplash.com/photo-abc?w=900",
        image_credit="A Photographer",
        image_credit_url="https://unsplash.com/@someone",
    )
    assert without.embedding_text() == with_photo.embedding_text()


def test_changing_a_photo_does_not_move_the_content_hash():
    """The receipt for "re-shooting the catalog costs no embedding calls"."""
    product = make_product()
    before = catalog.compute_content_hash(product)

    product.image_url = "https://images.unsplash.com/photo-xyz?w=900"
    product.image_credit = "Someone Else"
    assert catalog.compute_content_hash(product) == before

    # A real content edit still moves it, or the test above proves nothing.
    product.title = "Merino Crew Sweater, Reissued"
    assert catalog.compute_content_hash(product) != before


# --------------------------------------------------------------------------
# URL sanitising
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "value",
    [
        "javascript:alert(1)",
        "data:image/png;base64,iVBORw0KGgo=",
        "file:///etc/passwd",
        "//images.unsplash.com/photo-abc",  # protocol-relative, not http(s)
        "ftp://example.com/a.jpg",
        "   ",
        "",
        None,
    ],
)
def test_safe_url_rejects_anything_that_is_not_http(value):
    assert catalog.safe_url(value) == ""


@pytest.mark.parametrize(
    "value",
    [
        "https://images.unsplash.com/photo-abc?w=900&h=1125&fit=crop",
        "http://localhost:8000/static/img/a.jpg",
        "  https://example.com/a.jpg  ",
    ],
)
def test_safe_url_keeps_plain_http_urls(value):
    assert catalog.safe_url(value) == value.strip()


def test_safe_url_bounds_length():
    long_url = "https://example.com/" + "a" * 5000
    assert len(catalog.safe_url(long_url)) == catalog.MAX_URL_CHARS


async def _no_vector(db, product, *, force=False):  # noqa: ARG001
    """Stand-in for the vector dual-write, which needs a live Chroma + embeddings."""
    return True


def test_bad_image_url_degrades_instead_of_rejecting_the_edit(db, monkeypatch):
    """A junk URL costs the photo, not the rest of the admin's changes."""
    monkeypatch.setattr(catalog, "sync_product_vector", _no_vector)

    product = asyncio.run(
        catalog.create_product(
            db,
            {
                "title": "Merino Crew Sweater",
                "category": "Apparel",
                "price": 148.0,
                "image_url": "javascript:alert(1)",
                "image_credit": "A Photographer",
            },
        )
    )
    assert product.image_url == ""
    # Everything else on the same submission survived.
    assert product.title == "Merino Crew Sweater"
    assert product.price == 148.0
    assert product.image_credit == "A Photographer"


def test_image_fields_are_updatable_and_normalised(db, monkeypatch):
    monkeypatch.setattr(catalog, "sync_product_vector", _no_vector)

    product = asyncio.run(catalog.create_product(db, {"title": "Tee", "category": "Apparel"}))
    asyncio.run(
        catalog.update_product(
            db,
            product,
            {
                "image_url": "  https://images.unsplash.com/photo-abc?w=900  ",
                "image_credit": "  Ada   Lovelace  ",
                "image_credit_url": "https://unsplash.com/@ada",
            },
        )
    )
    assert product.image_url == "https://images.unsplash.com/photo-abc?w=900"
    # Whitespace collapsed, not merely stripped.
    assert product.image_credit == "Ada Lovelace"
    assert product.image_credit_url == "https://unsplash.com/@ada"


# --------------------------------------------------------------------------
# The presenter contract
# --------------------------------------------------------------------------
def test_image_url_is_always_present_in_the_card_shape():
    """One shape for the React component to test, absent or not."""
    plain = product_json(make_product())
    assert plain["image_url"] == ""

    shot = product_json(make_product(image_url="https://images.unsplash.com/p?w=900"))
    assert shot["image_url"] == "https://images.unsplash.com/p?w=900"


def test_credit_rides_only_with_the_detail_payload():
    product = make_product(
        image_url="https://images.unsplash.com/p?w=900",
        image_credit="A Photographer",
        image_credit_url="https://unsplash.com/@someone",
    )

    card = product_json(product)
    assert "image_credit" not in card
    assert "image_credit_url" not in card

    detail = product_json(product, full=True)
    assert detail["image_credit"] == "A Photographer"
    assert detail["image_credit_url"] == "https://unsplash.com/@someone"


def test_presenter_still_refuses_to_leak_unlisted_columns():
    """The field-by-field rule that keeps `password_hash`-shaped accidents out."""
    product = make_product()
    product.vector_error = "embedding failed: quota exceeded"
    product.content_hash = "deadbeef"

    data = product_json(product, full=True)
    assert "vector_error" not in data
    assert "content_hash" not in data
    assert "vector_state" not in data


# --------------------------------------------------------------------------
# The additive migration
# --------------------------------------------------------------------------
def test_additive_columns_are_added_to_a_database_that_predates_them():
    """The upgrade path: a products table built without the image columns."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    with engine.begin() as connection:
        # The old schema, as it stood before imagery existed.
        connection.exec_driver_sql(
            "CREATE TABLE products ("
            " id INTEGER PRIMARY KEY, title VARCHAR(255) NOT NULL,"
            " category VARCHAR(80), price FLOAT)"
        )
        connection.exec_driver_sql(
            "INSERT INTO products (title, category, price) VALUES ('Old Tee', 'Apparel', 28.0)"
        )

    with engine.begin() as connection:
        added = _apply_additive_columns(connection)

    assert sorted(added) == ["products.image_credit", "products.image_credit_url", "products.image_url"]

    with engine.connect() as connection:
        columns = {c["name"] for c in inspect(connection).get_columns("products")}
        assert {"image_url", "image_credit", "image_credit_url"} <= columns
        # The pre-existing row survived and reads as "no photograph".
        row = connection.execute(
            text("SELECT title, image_url FROM products")
        ).one()
        assert row.title == "Old Tee"
        assert row.image_url in ("", None)


def test_additive_columns_are_idempotent():
    """Second call must be a no-op, because init_db runs on every boot."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)

    with engine.begin() as connection:
        assert _apply_additive_columns(connection) == []
    with engine.begin() as connection:
        assert _apply_additive_columns(connection) == []


def test_every_additive_column_exists_on_the_model():
    """Catches the half-finished change: DDL listed, column never declared."""
    mapped = {c.name for c in Product.__table__.columns}
    for table, column, _ddl in ADDITIVE_COLUMNS:
        if table == "products":
            assert column in mapped, f"{column} is in ADDITIVE_COLUMNS but not on Product"


# --------------------------------------------------------------------------
# The seed rewriter
# --------------------------------------------------------------------------
def test_seed_formatter_round_trips_the_real_file_byte_for_byte():
    """The guarantee that keeps an image change reviewable."""
    module = _load_fetch_script()
    original = SEED_FILE.read_text(encoding="utf-8")
    assert module.dump_products(json.loads(original)) == original


def test_seed_formatter_keeps_lists_inline():
    module = _load_fetch_script()
    rendered = module.dump_products([{"title": "Tee", "tags": ["cotton", "basics"]}])
    assert '"tags": ["cotton", "basics"]' in rendered


def test_every_seeded_subcategory_gets_a_deliberate_query():
    """No product may fall through to a bare-subcategory search.

    Every pair needs either an explicit override or a category hint. Without one
    the query is just the subcategory name, and "Serums" or "Textiles" alone
    returns scenery. Note an override may legitimately *equal* the subcategory -
    "Headphones" is already a perfect product query - so this checks coverage,
    not that the string differs.
    """
    module = _load_fetch_script()
    rows = json.loads(SEED_FILE.read_text(encoding="utf-8"))

    uncovered = sorted(
        {
            f"{r['category']}|{r['subcategory']}"
            for r in rows
            if f"{r['category']}|{r['subcategory']}" not in module.QUERY_OVERRIDES
            and r["category"] not in module.CATEGORY_HINTS
        }
    )
    assert not uncovered, f"no override and no category hint for: {uncovered}"

    for row in rows:
        assert module.query_for(row["category"], row["subcategory"]).strip()


def test_ambiguous_subcategories_resolve_differently_per_category():
    """Accessories and Lighting exist under two categories and must not collide."""
    module = _load_fetch_script()
    assert module.query_for("Apparel", "Accessories") != module.query_for(
        "Electronics & Audio", "Accessories"
    )
    assert module.query_for("Home & Kitchen", "Lighting") != module.query_for(
        "Electronics & Audio", "Lighting"
    )


def test_query_overrides_all_refer_to_real_catalog_pairs():
    """An override for a pair that no longer exists is dead weight, and a typo
    in a key is invisible - it simply never fires."""
    module = _load_fetch_script()
    rows = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    real = {f"{r['category']}|{r['subcategory']}" for r in rows}

    stale = sorted(set(module.QUERY_OVERRIDES) - real)
    assert not stale, f"overrides for pairs not in the catalog: {stale}"
