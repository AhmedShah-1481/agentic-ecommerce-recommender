"""Seed the database and vector store, and optionally simulate user behaviour.

Usage:
    python -m scripts.seed
    python -m scripts.seed --simulate-user runner
    python -m scripts.seed --simulate-user audiophile --recommend
    python -m scripts.seed --reset

`--simulate-user` replays a realistic shopping session so the recommender can
be demonstrated end to end without clicking through the UI by hand.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import delete, select  # noqa: E402

from app.config import BASE_DIR  # noqa: E402
from app.db import init_db, session_scope  # noqa: E402
from app.models import (  # noqa: E402
    DigestLog,
    Event,
    LLMCall,
    Product,
    Recommendation,
    RecommendationItem,
    User,
    UserProfileState,
)
from app.security import hash_password  # noqa: E402
from app.services import catalog  # noqa: E402
from app.vector import store  # noqa: E402

SEED_FILE = BASE_DIR / "seed" / "products.json"

# How far back the seeded catalog is spread. Long enough that "newest" means
# something, short enough that nothing reads as abandoned stock.
CATALOG_WINDOW_DAYS = 60

DEMO_USERS = [
    {"email": "admin@bazario.dev", "password": "admin123", "name": "Ada Admin", "role": "admin"},
    {"email": "shopper@bazario.dev", "password": "shopper123", "name": "Sam Shopper", "role": "user"},
]

# Personas for --simulate-user. Each is a coherent shopping story: a set of
# searches, the categories they browse in, and the keywords that decide which
# products count as on-persona. `keywords` are matched as lowercase substrings
# against title + description + tags, so they must appear in the seed catalog.
PERSONAS = {
    "runner": {
        "searches": [
            "cushioned running shoes for long runs",
            "trail running shoe grip wet rock",
            "tempo trainer responsive foam",
            "recovery slides after a hard session",
        ],
        "category_focus": ["Footwear", "Apparel"],
        "keywords": ["running", "trail", "tempo", "cushion", "recovery",
                     "moisture wicking", "midlayer"],
    },
    "home-cook": {
        "searches": [
            "stainless steel saute pan",
            "cast iron dutch oven for braising",
            "sharp high carbon chef knife",
            "burr coffee grinder for espresso",
        ],
        "category_focus": ["Home & Kitchen"],
        "keywords": ["pan", "cookware", "knife", "cast iron", "braising", "coffee",
                     "cutting board", "kitchen prep"],
    },
    "audiophile": {
        "searches": [
            "open back headphones wide soundstage",
            "bookshelf speakers passive stereo",
            "noise cancelling earbuds for commuting",
            "headphone amplifier high impedance",
        ],
        "category_focus": ["Electronics & Audio"],
        "keywords": ["headphone", "speaker", "earbuds", "hi-fi", "amplifier",
                     "soundstage", "monitoring"],
    },
    "skincare": {
        "searches": [
            "ceramide moisturiser barrier repair",
            "vitamin c serum for brightening",
            "gentle cleanser for sensitive skin",
            "mineral sunscreen spf 50",
        ],
        "category_focus": ["Beauty"],
        "keywords": ["moisturiser", "serum", "cleanser", "sunscreen", "retinal",
                     "ceramide", "barrier repair"],
    },
}


async def seed_products(reset: bool = False) -> int:
    if not SEED_FILE.exists():
        raise SystemExit(f"Seed file missing: {SEED_FILE}")

    data = json.loads(SEED_FILE.read_text(encoding="utf-8"))

    with session_scope() as db:
        if reset:
            db.execute(delete(RecommendationItem))
            db.execute(delete(Recommendation))
            db.execute(delete(Event))
            db.execute(delete(Product))
            db.commit()
            store.reset()

        existing = {t for t in db.scalars(select(Product.title))}
        created = 0
        for row in data:
            if row["title"] in existing:
                continue
            await catalog.create_product(db, row)
            created += 1
            print(f"  + {row['title'][:62]}")

    return created


def _interleave_by_category(products: list[Product]) -> list[Product]:
    """Round-robin the catalog across its categories, newest first.

    Newest first rather than oldest first because shelves are uneven: the short
    ones run dry and whatever is left clumps at the end of the ordering. Putting
    that clump at the *old* end means the front - the part "New arrivals" shows -
    is one product per category, which is the whole point of interleaving.

    Deterministic: categories keep the order they are first seen in, and within a
    category later-in-the-file reads as newer, so a re-seed reproduces the same
    arrangement.
    """
    shelves: dict[str, list[Product]] = {}
    for product in products:
        shelves.setdefault(product.category, []).append(product)

    ordered: list[Product] = []
    while any(shelves.values()):
        for shelf in shelves.values():
            if shelf:
                ordered.append(shelf.pop())
    return ordered


def backdate_catalog() -> int:
    """Spread `created_at` across the seeded catalog.

    The seed writes every product inside a couple of seconds, so `sort=newest`
    collapses into reverse insert order - and because the seed file is grouped by
    category, that means the whole "New arrivals" strip comes from whichever
    category happens to be last in the file. Backdating over a plausible window,
    interleaved across categories, makes the newest rows a genuine mix.

    Only touches titles that came from the seed file: a product added by hand
    through the admin form keeps its real creation date.
    """
    titles = {row["title"] for row in json.loads(SEED_FILE.read_text(encoding="utf-8"))}

    with session_scope() as db:
        products = [
            product
            for product in db.scalars(select(Product).order_by(Product.id))
            if product.title in titles
        ]
        if not products:
            return 0

        ordered = _interleave_by_category(products)
        # `created_at` is a naive column and the rest of the table holds naive UTC
        # (the model default is aware, and SQLite drops the offset on the way in),
        # so match that rather than writing local time into one column.
        newest = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1)
        step = timedelta(days=CATALOG_WINDOW_DAYS) / max(len(ordered) - 1, 1)
        for index, product in enumerate(ordered):
            product.created_at = newest - step * index
        db.commit()

    return len(ordered)


def seed_users() -> None:
    with session_scope() as db:
        for spec in DEMO_USERS:
            user = db.scalars(select(User).where(User.email == spec["email"])).first()
            if user is None:
                db.add(
                    User(
                        email=spec["email"],
                        password_hash=hash_password(spec["password"]),
                        name=spec["name"],
                        role=spec["role"],
                    )
                )
                print(f"  + {spec['role']:5} {spec['email']}  (password: {spec['password']})")
        db.commit()


def simulate_user(persona_name: str, email: str = "shopper@bazario.dev") -> int:
    """Replay a believable shopping session for one persona."""
    persona = PERSONAS.get(persona_name)
    if persona is None:
        raise SystemExit(f"Unknown persona {persona_name!r}. Options: {', '.join(PERSONAS)}")

    random.seed(f"{persona_name}-fixed")  # reproducible demos

    with session_scope() as db:
        user = db.scalars(select(User).where(User.email == email)).first()
        if user is None:
            raise SystemExit(f"No such user: {email}. Run the seeder first.")

        products = list(db.scalars(select(Product).where(Product.is_published.is_(True))))

        def matches(product: Product) -> bool:
            blob = f"{product.title} {product.description} {' '.join(product.tags or [])}".lower()
            in_category = product.category in persona["category_focus"]
            has_keyword = any(k in blob for k in persona["keywords"])
            return in_category and has_keyword

        targets = [p for p in products if matches(p)]
        if not targets:
            targets = [p for p in products if p.category in persona["category_focus"]]
        distractors = [p for p in products if p not in targets]

        # Clear this user's history so personas don't blend across runs.
        db.execute(delete(Event).where(Event.user_id == user.id))
        db.execute(delete(RecommendationItem).where(
            RecommendationItem.rec_id.in_(
                select(Recommendation.id).where(Recommendation.user_id == user.id)
            )
        ))
        db.execute(delete(Recommendation).where(Recommendation.user_id == user.id))
        state = db.get(UserProfileState, user.id)
        if state is not None:
            db.delete(state)
        db.commit()

        session_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        rows: list[Event] = []
        clock = now - timedelta(hours=3)

        def add(event_type: str, *, product=None, query="", dwell=0, scroll=0) -> None:
            nonlocal clock
            clock += timedelta(seconds=random.randint(20, 150))
            rows.append(
                Event(
                    event_id=str(uuid.uuid4()),
                    user_id=user.id,
                    session_id=session_id,
                    event_type=event_type,
                    product_id=product.id if product is not None else None,
                    search_query=query,
                    path=f"/product/{product.id}" if product is not None else "/",
                    dwell_ms=dwell,
                    scroll_pct=scroll,
                    meta={"simulated": True},
                    client_ts=clock,
                    server_ts=clock,
                )
            )

        add("page_view")
        # Searches, each followed by browsing the results.
        for query in persona["searches"]:
            add("search", query=query)
            for product in random.sample(targets, k=min(2, len(targets))):
                add("product_click", product=product)
                add("product_view", product=product)
                add("dwell", product=product, dwell=random.randint(35_000, 140_000), scroll=random.choice([50, 75, 100]))

        # Returns to the two strongest matches - the repeat-visit signal.
        for product in targets[:2]:
            add("product_view", product=product)
            add("dwell", product=product, dwell=random.randint(60_000, 180_000), scroll=100)

        # A little noise, so the profile isn't artificially clean.
        for product in random.sample(distractors, k=min(3, len(distractors))):
            add("product_view", product=product)
            add("dwell", product=product, dwell=random.randint(3_000, 9_000), scroll=25)

        if targets:
            add("add_to_cart", product=targets[0])

        db.add_all(rows)
        state = UserProfileState(
            user_id=user.id,
            events_since_last_rec=sum(
                1 for r in rows
                if r.event_type in {"product_view", "search", "product_click", "add_to_cart", "dwell"}
            ),
        )
        db.add(state)
        db.commit()

        print(f"  simulated {len(rows)} events for {email} as '{persona_name}'")
        print(f"  focus categories: {', '.join(persona['category_focus'])}")
        print(f"  matched {len(targets)} on-persona products")
        return user.id


async def run_recommendation(user_id: int) -> None:
    from app.services import recommendations as rec_service

    print("\n  running agent...")
    reason = await rec_service.maybe_generate(user_id, force=True)
    print(f"  decision: {reason}")

    with session_scope() as db:
        rec = rec_service.get_current(db, user_id)
        if rec is None:
            print("  no recommendation produced")
            return
        print(f"\n  HEADLINE : {rec.headline}")
        print(f"  NARRATIVE: {rec.narrative}")
        print(f"  model    : {rec.model} | fallback={rec.is_fallback}")
        print(f"  path     : {' -> '.join(rec.agent_trace.get('path', []))}")
        print(f"  queries  : {rec.agent_trace.get('queries')}")
        print("  ITEMS:")
        for item in rec.items:
            product = db.get(Product, item.product_id)
            print(f"    {item.rank}. [{product.category}] {product.title}")
            print(f"       -> {item.reason}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed Bazario Mart")
    parser.add_argument("--reset", action="store_true", help="wipe products/events first")
    parser.add_argument("--simulate-user", dest="persona", help=f"one of: {', '.join(PERSONAS)}")
    parser.add_argument("--recommend", action="store_true", help="run the agent after simulating")
    parser.add_argument("--email", default="shopper@bazario.dev")
    args = parser.parse_args()

    print("Initialising database...")
    init_db()

    print("\nSeeding users...")
    seed_users()

    print("\nSeeding catalog (dual-write to SQL + vector store)...")
    created = asyncio.run(seed_products(reset=args.reset))
    print(f"  {created} product(s) created")

    dated = backdate_catalog()
    print(f"  {dated} product(s) dated across the last {CATALOG_WINDOW_DAYS} days")

    with session_scope() as db:
        status = catalog.sync_status(db)
    print(f"\nSync: sql={status['sql_count']} vector={status['vector_count']} "
          f"in_sync={status['in_sync']} provider={status['provider']}")

    user_id = None
    if args.persona:
        print(f"\nSimulating '{args.persona}' behaviour...")
        user_id = simulate_user(args.persona, args.email)

    if args.recommend:
        if user_id is None:
            with session_scope() as db:
                user = db.scalars(select(User).where(User.email == args.email)).first()
                user_id = user.id if user else None
        if user_id:
            asyncio.run(run_recommendation(user_id))

    print("\nDone.")


if __name__ == "__main__":
    main()
