"""The storefront JSON API the React app reads.

Three of these are contracts a server-side reader cannot check by inspection,
which is why they are pinned rather than trusted:

  * **The filter set and the count must agree.** `_apply_filters` builds the
    predicates for the page of rows *and* for the total. Two hand-maintained
    WHERE clauses would drift, and the symptom is subtle: a pagination control
    that offers a page four which comes back empty.

  * **`sort` is whitelisted.** An unknown key must fall back to the default, not
    500 and not reach the query builder as a column name.

  * **Unpublished is indistinguishable from absent.** A hidden product answering
    403 while a missing one answers 404 turns the detail endpoint into an oracle
    for what is in the catalog but not for sale yet.

Runs without lifespan: `TestClient(app)` outside a `with` block never starts the
event consumer or the scheduler, and nothing here needs either.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.main import app
from app.models import Product
from app.routers.storefront import SORTS
from app.services.presenters import blurb

# A catalog small enough to assert on exactly, spread over the axes the sidebar
# filters on: two categories, three brands, a price range, and one unpublished
# row that must stay invisible everywhere.
CATALOG = [
    dict(title="Trail Runner GTX", category="Footwear", subcategory="Running",
         brand="Kestrelane", price=140.0, rating=4.8, color="Slate",
         tags=["running", "trail"], size="10",
         description="A cushioned trail shoe. Holds its shape over long distances."),
    dict(title="Recovery Slide", category="Footwear", subcategory="Sandals",
         brand="Kestrelane", price=45.0, rating=4.2, color="Black",
         tags=["recovery"], size="10",
         description="Worn after a run."),
    dict(title="Merino Crew Sweater", category="Apparel", subcategory="Knitwear",
         brand="Sundermere", price=148.0, rating=4.7, color="Oatmeal",
         tags=["merino", "wool"], size="M",
         description="Extra-fine merino, warm without bulk."),
    dict(title="Heavyweight Tee", category="Apparel", subcategory="T-Shirts",
         brand="Stonebrook", price=28.0, rating=4.5, color="Fog Grey",
         tags=["cotton"], size="M",
         description="240gsm combed cotton."),
    dict(title="Discontinued Parka", category="Apparel", subcategory="Outerwear",
         brand="Stonebrook", price=310.0, rating=5.0, color="Olive",
         tags=["archive"], size="L", description="Not for sale.",
         is_published=False),
]


@pytest.fixture()
def db():
    # StaticPool + check_same_thread: TestClient runs the app on a worker thread
    # and the default pool for in-memory SQLite hands out a connection per
    # thread, which would put the request on a second, empty database.
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    session.add_all([Product(**row) for row in CATALOG])
    session.commit()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def client(db):
    app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_db, None)


def titles(body: dict) -> list[str]:
    return [p["title"] for p in body["products"]]


# --------------------------------------------------------------------------
# GET /api/catalog
# --------------------------------------------------------------------------
def test_catalog_returns_only_published_products(client):
    body = client.get("/api/catalog").json()

    assert body["total"] == 4
    assert "Discontinued Parka" not in titles(body)


def test_default_sort_is_rating_descending(client):
    body = client.get("/api/catalog").json()

    assert titles(body) == [
        "Trail Runner GTX",      # 4.8
        "Merino Crew Sweater",   # 4.7
        "Heavyweight Tee",       # 4.5
        "Recovery Slide",        # 4.2
    ]


@pytest.mark.parametrize(
    ("sort", "expected"),
    [
        ("price_asc", ["Heavyweight Tee", "Recovery Slide", "Trail Runner GTX", "Merino Crew Sweater"]),
        ("price_desc", ["Merino Crew Sweater", "Trail Runner GTX", "Recovery Slide", "Heavyweight Tee"]),
    ],
)
def test_price_sorts_run_in_both_directions(client, sort, expected):
    assert titles(client.get(f"/api/catalog?sort={sort}").json()) == expected


def test_an_unknown_sort_falls_back_to_the_default(client):
    """Whitelisted, not interpolated. A sort key that reached the query builder as
    a column name would be an injection surface, and a client sending a stale key
    should see the default rather than a 500."""
    body = client.get("/api/catalog?sort=price;DROP TABLE products").json()

    assert body["sort"] == "featured"
    assert body["total"] == 4


def test_every_advertised_sort_key_actually_works(client):
    """`/api/catalog/facets` publishes the sort vocabulary the client renders its
    dropdown from. A key in that list that the catalog endpoint rejects would give
    the user a control that silently does nothing."""
    advertised = client.get("/api/catalog/facets").json()["sorts"]

    assert advertised == list(SORTS)
    for key in advertised:
        body = client.get(f"/api/catalog?sort={key}").json()
        assert body["sort"] == key, f"{key} was not honoured"
        assert body["total"] == 4


def test_search_matches_across_the_text_fields(client):
    """One term, four different columns - a shopper does not know or care which
    field the word they typed lives in."""
    assert titles(client.get("/api/catalog?q=merino").json()) == ["Merino Crew Sweater"]      # tag
    assert titles(client.get("/api/catalog?q=kestrelane").json()) == [
        "Trail Runner GTX", "Recovery Slide",                                                 # brand
    ]
    assert titles(client.get("/api/catalog?q=oatmeal").json()) == ["Merino Crew Sweater"]     # colour
    assert titles(client.get("/api/catalog?q=combed cotton").json()) == ["Heavyweight Tee"]   # description


def test_search_is_case_insensitive(client):
    assert titles(client.get("/api/catalog?q=MERINO").json()) == ["Merino Crew Sweater"]


def test_facet_filters_narrow_and_combine(client):
    assert client.get("/api/catalog?category=Footwear").json()["total"] == 2
    assert client.get("/api/catalog?brand=Stonebrook").json()["total"] == 1  # parka is hidden
    assert client.get("/api/catalog?category=Apparel&brand=Sundermere").json()["total"] == 1
    assert client.get("/api/catalog?category=Footwear&brand=Sundermere").json()["total"] == 0


def test_price_and_rating_bounds_are_inclusive(client):
    """Inclusive matters for a slider: dragging the maximum to the catalog's own
    top price must not exclude the product that set it."""
    assert titles(client.get("/api/catalog?min_price=148").json()) == ["Merino Crew Sweater"]
    assert titles(client.get("/api/catalog?max_price=28").json()) == ["Heavyweight Tee"]
    assert client.get("/api/catalog?min_price=45&max_price=140").json()["total"] == 2
    assert titles(client.get("/api/catalog?min_rating=4.7").json()) == [
        "Trail Runner GTX", "Merino Crew Sweater",
    ]


def test_the_total_is_counted_through_the_same_filters_as_the_page(client):
    """The bug this exists for: a total computed over the unfiltered catalog makes
    the client render pagination for pages that come back empty."""
    body = client.get("/api/catalog?category=Footwear&page_size=1").json()

    assert body["total"] == 2      # not 4
    assert body["pages"] == 2
    assert len(body["products"]) == 1


def test_pagination_walks_the_result_set_without_gaps_or_repeats(client):
    first = client.get("/api/catalog?page_size=3&page=1").json()
    second = client.get("/api/catalog?page_size=3&page=2").json()

    assert (first["has_prev"], first["has_next"]) == (False, True)
    assert (second["has_prev"], second["has_next"]) == (True, False)
    assert titles(first) + titles(second) == [
        "Trail Runner GTX", "Merino Crew Sweater", "Heavyweight Tee", "Recovery Slide",
    ]


def test_pages_divides_exactly_on_a_multiple(client):
    """Ceiling division is reported by the server so the client cannot round it
    differently. 4 items at 2 per page is 2 pages, not 3."""
    assert client.get("/api/catalog?page_size=2").json()["pages"] == 2


def test_out_of_range_page_and_size_are_clamped_not_rejected(client):
    """A stale bookmark or a hand-edited query string is not an error - but the
    response has to report what the server actually did, because the client draws
    its controls from those numbers."""
    body = client.get("/api/catalog?page=0&page_size=0").json()
    assert (body["page"], body["page_size"]) == (1, 1)

    body = client.get("/api/catalog?page_size=9999").json()
    assert body["page_size"] == 48

    beyond = client.get("/api/catalog?page=99").json()
    assert beyond["products"] == []
    assert beyond["total"] == 4        # the set still exists, this page does not
    assert beyond["has_next"] is False


def test_no_match_is_an_empty_page_not_a_404(client):
    """The empty-search-results state is a design element on the client, so it
    needs a 200 with zero products rather than an error to branch on."""
    response = client.get("/api/catalog?q=zzzzz")

    assert response.status_code == 200
    body = response.json()
    assert (body["products"], body["total"], body["pages"]) == ([], 0, 0)


def test_the_card_shape_carries_what_a_card_draws(client):
    """Grid cards render a generated visual panel keyed on category and brand and
    show size/colour, so those have to be in the list payload - not just on the
    detail response."""
    card = client.get("/api/catalog?q=merino").json()["products"][0]

    assert set(card) == {
        "id", "title", "category", "subcategory", "brand",
        "price", "rating", "tags", "size", "color", "blurb",
    }
    # Trimmed prose on a card, never the full description.
    assert "description" not in card


# --------------------------------------------------------------------------
# GET /api/catalog/facets
# --------------------------------------------------------------------------
def test_facets_count_only_published_rows(client):
    body = client.get("/api/catalog/facets").json()

    assert body["categories"] == [
        {"value": "Apparel", "count": 2},    # the parka is not counted
        {"value": "Footwear", "count": 2},
    ]
    assert body["total"] == 4


def test_facet_brands_are_sorted_and_counted(client):
    brands = client.get("/api/catalog/facets").json()["brands"]

    assert brands == [
        {"value": "Kestrelane", "count": 2},
        {"value": "Stonebrook", "count": 1},
        {"value": "Sundermere", "count": 1},
    ]


def test_price_bounds_come_from_the_published_catalog(client):
    """The slider has to end where the catalog does. The 310.0 parka is hidden, so
    a maximum that included it would leave a dead third of the track."""
    price = client.get("/api/catalog/facets").json()["price"]

    assert price == {"min": 28.0, "max": 148.0}


def test_facets_survive_an_empty_catalog(client, db):
    """A fresh clone that has not run the seed yet still has to render a
    sidebar - `min()` over no rows is NULL, and the client does slider arithmetic
    on those numbers."""
    db.query(Product).delete()
    db.commit()

    body = client.get("/api/catalog/facets").json()

    assert body["categories"] == []
    assert body["price"] == {"min": 0.0, "max": 0.0}
    assert body["total"] == 0


# --------------------------------------------------------------------------
# GET /api/products/{id}
# --------------------------------------------------------------------------
def test_product_detail_returns_the_full_description(client, db):
    product_id = db.query(Product).filter_by(title="Merino Crew Sweater").one().id

    body = client.get(f"/api/products/{product_id}").json()

    assert body["product"]["title"] == "Merino Crew Sweater"
    assert body["product"]["description"] == "Extra-fine merino, warm without bulk."
    assert body["product"]["tags"] == ["merino", "wool"]


def test_related_products_share_the_category_and_exclude_self(client, db):
    product_id = db.query(Product).filter_by(title="Trail Runner GTX").one().id

    related = client.get(f"/api/products/{product_id}").json()["related"]

    assert [p["title"] for p in related] == ["Recovery Slide"]


def test_related_never_includes_an_unpublished_product(client, db):
    """The parka is the highest-rated Apparel row in the catalog, so a related
    strip that forgot the published filter would put it first."""
    product_id = db.query(Product).filter_by(title="Heavyweight Tee").one().id

    related = client.get(f"/api/products/{product_id}").json()["related"]

    assert [p["title"] for p in related] == ["Merino Crew Sweater"]


def test_an_unpublished_product_is_a_404_not_a_403(client, db):
    """Indistinguishable from absent, deliberately: a 403 would confirm the id
    exists and tell an anonymous caller something is in the catalog but not yet
    for sale."""
    hidden = db.query(Product).filter_by(title="Discontinued Parka").one().id
    missing = client.get("/api/products/999999")

    assert client.get(f"/api/products/{hidden}").status_code == 404
    assert missing.status_code == 404
    assert client.get(f"/api/products/{hidden}").json() == missing.json()


# --------------------------------------------------------------------------
# CORS - the storefront is cross-origin and sends the session cookie
# --------------------------------------------------------------------------
def test_the_dev_storefront_origin_is_allowed_with_credentials(client):
    """`allow_credentials=True` is the whole point: the storefront authenticates
    with the same signed cookie the Jinja pages use. A browser will not attach it
    unless the response says credentials are allowed."""
    response = client.get("/api/catalog", headers={"Origin": "http://localhost:5173"})

    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert response.headers["access-control-allow-credentials"] == "true"


def test_the_allowed_origin_is_never_a_wildcard(client):
    """A wildcard would not loosen anything - it fails outright. Browsers reject
    `Access-Control-Allow-Origin: *` on a credentialed request, so the storefront
    would simply stop being able to sign in."""
    response = client.get("/api/catalog", headers={"Origin": "http://localhost:5173"})

    assert response.headers["access-control-allow-origin"] != "*"


def test_an_unlisted_origin_gets_no_cors_headers(client):
    response = client.get("/api/catalog", headers={"Origin": "http://evil.example"})

    assert "access-control-allow-origin" not in response.headers


def test_the_preflight_for_a_json_post_is_answered(client):
    """The React client POSTs JSON, which is not a simple request - the browser
    sends OPTIONS first and never issues the POST if that fails."""
    response = client.options(
        "/api/auth/login",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert "POST" in response.headers["access-control-allow-methods"]


# --------------------------------------------------------------------------
# The blurb helper
# --------------------------------------------------------------------------
def test_blurb_prefers_a_whole_sentence(client):
    text = "A cushioned trail shoe. Holds its shape over long distances."

    assert blurb(text) == "A cushioned trail shoe."


def test_blurb_truncates_on_a_word_boundary_when_there_is_no_sentence_break(client):
    text = "a" * 20 + " " + "b" * 200

    result = blurb(text, limit=40)

    assert result.endswith("…")
    assert "b" not in result  # cut before the long second word, not mid-word


def test_blurb_leaves_short_text_alone(client):
    assert blurb("Worn after a run.") == "Worn after a run."
    assert blurb("") == ""


def test_a_long_first_sentence_is_truncated_rather_than_returned_whole(client):
    """The sentence-boundary shortcut only applies inside the budget - otherwise a
    single 400-character sentence would come back untrimmed and break the card."""
    text = "word " * 100 + "end."

    result = blurb(text, limit=60)

    assert len(result) <= 61  # 60 plus the ellipsis
    assert result.endswith("…")
