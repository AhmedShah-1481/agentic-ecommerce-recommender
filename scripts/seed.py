"""Seed the database and vector store, and optionally simulate user behaviour.

Usage:
    python -m scripts.seed
    python -m scripts.seed --simulate-user agentic
    python -m scripts.seed --simulate-user data --recommend
    python -m scripts.seed --reset

`--simulate-user` replays a realistic browsing session so the recommender can
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

DEMO_USERS = [
    {"email": "admin@learnloop.dev", "password": "admin123", "name": "Ada Admin", "role": "admin"},
    {"email": "learner@learnloop.dev", "password": "learner123", "name": "Sam Learner", "role": "user"},
]

# Personas for --simulate-user. Each is a coherent browsing story.
PERSONAS = {
    "agentic": {
        "searches": ["agentic ai", "langgraph agents", "ai agent tool calling", "agent memory"],
        "category_focus": ["AI Engineering", "LLM Engineering"],
        "keywords": ["agent", "agentic", "langgraph", "tool", "orchestrat", "reasoning"],
    },
    "data": {
        "searches": ["data pipelines", "spark etl", "airflow orchestration", "warehouse modelling"],
        "category_focus": ["Data Engineering", "MLOps"],
        "keywords": ["data", "etl", "pipeline", "spark", "warehouse", "airflow", "stream"],
    },
    "rag": {
        "searches": ["vector database", "rag chunking", "hybrid search", "reranking"],
        "category_focus": ["RAG & Search"],
        "keywords": ["rag", "retrieval", "vector", "embedding", "search", "rerank", "chunk"],
    },
    "security": {
        "searches": ["application security", "threat modelling", "pentesting web apps"],
        "category_focus": ["Security"],
        "keywords": ["security", "secure", "threat", "pentest", "vulnerab", "auth"],
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


def simulate_user(persona_name: str, email: str = "learner@learnloop.dev") -> int:
    """Replay a believable browsing session for one persona."""
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
        print(f"  matched {len(targets)} on-persona courses")
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
    parser = argparse.ArgumentParser(description="Seed LearnLoop")
    parser.add_argument("--reset", action="store_true", help="wipe products/events first")
    parser.add_argument("--simulate-user", dest="persona", help=f"one of: {', '.join(PERSONAS)}")
    parser.add_argument("--recommend", action="store_true", help="run the agent after simulating")
    parser.add_argument("--email", default="learner@learnloop.dev")
    args = parser.parse_args()

    print("Initialising database...")
    init_db()

    print("\nSeeding users...")
    seed_users()

    print("\nSeeding catalog (dual-write to SQL + vector store)...")
    created = asyncio.run(seed_products(reset=args.reset))
    print(f"  {created} product(s) created")

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
