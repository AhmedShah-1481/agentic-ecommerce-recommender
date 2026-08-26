# Project state — Bazario Mart

Working notes for picking this up cold. Read [README.md](README.md) for what the
project *is*; this file is about where it stands, what's still open, and why
things were built the way they were.

**Status: the platform is complete — all requirements plus all four bonuses,
169 tests passing.** It began life as *LearnLoop*, a course recommender, and was
converted to a general e-commerce store in three reviewable steps, all now done:

| Step | What | State |
|---|---|---|
| 1 | Domain swap: courses → products | **done** |
| 2 | Mesh API → Gemini (`google-genai`) | **done** |
| 3 | React + Tailwind storefront, Jinja2 kept for `/admin/*` | **done** |

Step 1 renamed the domain everywhere except `app/ai/` (Step 2's territory) and
the frontend (Step 3's). `services/events.py`, `triggers.py` and `signal.py` were
left untouched on purpose: cart-adds, dwell and repeat search already *are*
retail signals, so there was nothing domain-specific in them to rename.

Step 2 replaced the Mesh client outright — `app/ai/mesh_client.py` is deleted,
not adapted — and with a real key in `.env` the agent is now genuinely
model-driven for the first time: real Gemini vectors for retrieval and
model-authored narrative, both verified end to end (§1, *The agent*).

Step 3 moved every customer-facing page to a React + Vite + Tailwind app in
[frontend/](frontend/) and left `/admin/*` and `/inbox` on Jinja. The customer
HTML routes are gone rather than kept alongside — `catalog.html`,
`dashboard.html`, `product.html` and `_signal.html` are deleted, and `/` is a
JSON signpost now. Two processes in dev (§3), one origin each, one signed cookie
shared between them (§5). **This means the app is no longer browsable at
:8000 alone** — that URL serves the API, the admin panel and the inbox; the store
is at :5173.

---

## 1 · Done and verified

Every item below was checked by running it, not by reading the code.

### The platform
- FastAPI + SQLAlchemy 2.0 + SQLite (WAL mode), with Jinja2 still serving the
  admin panel, the auth forms and `/inbox`.
- bcrypt password hashing (bcrypt 5.0.0 directly, no passlib), signed httponly
  session cookie via `itsdangerous`.
- Two roles: `user` and `admin`, enforced by `current_user` / `api_user` /
  `require_admin` dependencies in [app/security.py](app/security.py).
- 8 related tables in [app/models.py](app/models.py): users, products, events,
  user_profile_state, recommendations, recommendation_items, llm_calls,
  digest_log.
- **API (`:8000`).** JSON: `GET /api/catalog`, `/api/catalog/facets`,
  `/api/products/{id}` ([storefront.py](app/routers/storefront.py));
  `GET /api/recommendations`, `/api/me/signal`,
  `POST /api/recommendations/refresh` ([recs.py](app/routers/recs.py));
  `GET /api/auth/me`, `POST /api/auth/{login,register,logout}`;
  `POST /api/events`, `POST /api/events/beacon`, `GET /api/events/stats`.
  Jinja: `/admin/*`, `/login`, `/register`, `/inbox`. `GET /` is a signpost that
  answers with the storefront URL rather than a catalog.
- Admin views (Jinja, unchanged by Step 3): product CRUD at `/admin`,
  `/admin/sync`, `/admin/ai-usage`, `/admin/traces`, `/admin/events`,
  `/admin/scheduler`.
- **Storefront (`:5173`).** React 19 + Vite + Tailwind v4 in
  [frontend/](frontend/): `/`, `/shop`, `/product/:id`, `/for-you`, `/cart`,
  `/login`, `/register`, and a 404. Details below.
- **Verified:** all routes return expected status codes under `TestClient`
  (`/healthz` 200, `/` 200, `/login` 200, `/admin` 200 as admin, `/inbox` 303
  when anonymous, `POST /api/events` 202, `GET /api/catalog` 200).

### The storefront
- [frontend/](frontend/) — React 19.2, react-router-dom 7, Tailwind v4 (CSS-first
  `@theme`, so there is no `tailwind.config.js`), `lucide-react` for icons, Vite 8.
  No component library: the whole point was that it should not look like every
  other generated app.
- **Design language** is declared once, in
  [frontend/src/index.css](frontend/src/index.css): bone ground (`#FAF7F2`) rather
  than white, ink (`#16150F`) rather than black, one terracotta accent
  (`#B8502F`), Fraunces for display and Inter for text, 2px card radii. Every
  colour a component uses is a token there — nothing hardcodes a hex except
  `productVisual.js`, which generates imagery and owns its own palettes.
- **There are no product photographs and the app does not pretend otherwise.**
  [ProductVisual.jsx](frontend/src/components/ProductVisual.jsx) draws a
  deterministic duotone panel per product — hue from the category, one of four
  compositions and a glyph from the id — so the grid has real visual rhythm with
  no image host, no placeholder-photo service and no schema change. Six shelves
  read as six colours, which is deliberate: on a related-products strip all four
  items share a hue, and the glyph plus the subcategory label carry the
  difference.
- **Every state is drawn, not just the happy path**: loading *skeletons* rather
  than spinners (sized to the API's `PAGE_SIZE` so the layout does not jump),
  empty cart, empty search with a way out, a friendly cold start on *For you* for
  a user the agent has nothing to say about yet, a signed-out state on the same
  page that explains rather than redirects, and an error state carrying the
  API's own message.
- **"For you" is deliberately not the catalog grid.** The per-item agent reason
  renders as a visible annotation under each card — a terracotta-ruled note, not
  a tooltip — beside the narrative in an ink band, and *Your signal* lists the
  behaviour that produced it. That is the whole product; hiding it behind a hover
  would have buried it.
- **Verified visually, not just by build**: 15 states captured over CDP from
  headless Chrome at 1440px and 430px (home, shop, shop filtered, empty search,
  product, cart, empty cart, login, 404, *For you* signed in and signed out, plus
  mobile home/shop/cart/for-you). Mobile reflows rather than shrinks — the filter
  sidebar becomes a sheet, the nav a drawer — and no page scrolls horizontally.
  Three real defects were found that way and fixed: cart line thumbnails
  collapsing to 0px (`ProductVisual` has no intrinsic height and needs an
  aspect), `Empty`'s secondary link rendering *above* the primary CTA, and the
  grid skeleton's row count disagreeing with the page size.


### Dual-write (SQL + vector DB)
- [app/services/catalog.py](app/services/catalog.py) owns every mutation so the
  two stores can't drift. Chroma 1.5.9, persistent, cosine space,
  app-supplied embeddings.
- `content_hash` fingerprints the embedded text, so a **price-only edit skips
  re-embedding entirely**.
- A failed vector write still commits the SQL row with
  `vector_state='pending'`; `reconcile()` repairs it rather than 500-ing.
- `reconcile()` runs at startup and every 5 min: retries pending rows, deletes
  orphan vectors, re-embeds rows whose fingerprint or **embedding provider**
  disagrees with what's stored, and **resets the collection when the vector
  *width* changes** — see §5, *Vector width is part of the provider identity*.
- **Verified:** `sql=56 vector=56 in_sync=True`, provider honestly reported as
  `gemini:gemini-embedding-001@768`, reconcile repairs 0 on a clean store. The
  512→768 reset was watched happening live, not reasoned about: one run reported
  `dimension_reset={'from': 512, 'to': 768}`, `reembedded=56`,
  `still_failing=0`, 43s.

### Behavioural tracking
- [frontend/src/lib/tracker.js](frontend/src/lib/tracker.js), ~460 lines, no deps.
  **The vanilla `app/static/js/tracker.js` and `ui.js` are deleted**, not kept in
  parallel — the pages they instrumented no longer exist, and two trackers would
  have meant two contracts. The transport is carried over unchanged, because that
  is where the guarantees the backend depends on live: queue flushed on 15 events
  / 5s idle / tab hidden / unload, `sendBeacon` on unload and `fetch(keepalive)`
  otherwise.
- Throttling: scroll depth once each at 25/50/75/100%, search debounced 600ms,
  dwell stops accruing on a backgrounded tab, `MIN_DWELL_MS = 1500` so a bounce
  is not engagement.
- Client-generated `event_id` for idempotency; `localStorage` backup merges and
  dedupes, and only server-confirmed ids are cleared.
- **Three things had to change for a single-page app**, and each was a real bug
  before it was a design note:
  - *Context is pushed, not scraped.* The MPA read `[data-ll-page]` out of the
    DOM on load. `RouteTracker` in [frontend/src/App.jsx](frontend/src/App.jsx)
    calls `startPage()` on every pathname change instead, so a page cannot forget
    to be instrumented and `product_view` is emitted from the one place that
    knows a navigation happened.
  - *A route change is a page boundary.* Client-side navigation fires no unload,
    so `startPage()` closes the previous page out — shipping its dwell, resetting
    the scroll milestones — before opening the next. Without it one tab's dwell
    grows all session and exactly four scroll events are ever recorded.
    `lastTrackedPath` is module-level rather than a ref, because StrictMode
    mounts effects twice and a component-scoped guard still lets the second mount
    emit a duplicate `page_view`.
  - *Cross-origin transport.* `credentials: "include"`, or the cookie is stripped
    and every event lands anonymous — the pipeline would look healthy while
    attributing nothing to the signed-in user. The beacon body goes as
    `text/plain`, a CORS-safelisted type needing no preflight, since a preflight
    on unload is exactly the request a browser abandons; `POST /api/events/beacon`
    parses the body without trusting the content-type for that reason, and always
    answers 2xx because a beacon cannot be retried.
- Ingest ([app/routers/events.py](app/routers/events.py)) **opens no database
  connection at all** — the user id comes from the signed cookie
  (`optional_user_id`), not a lookup, because this is the busiest endpoint.
  Returns 202; a background consumer writes batches with one multi-row INSERT.
- Bounded queue sheds *oldest* events under flood; shutdown flushes what's
  queued.
- **Verified:** `POST /api/events` 202, events land in the DB via the consumer,
  per-type counts visible at `/admin/events`, and the running storefront's
  *Your signal* panel shows the rows arriving with the right labels.
- Catalog **facet selections are tracked** (`category` / `brand`). This was a
  real blind spot found by browsing the running app: choosing a category
  navigates, and the tracker classifies that as a bare `page_view` — which is
  excluded from `MEANINGFUL_TYPES`, carries no `product_id`, and the facet name
  only ever exists in the query string, which the tracker never reads. Filtering
  through seven categories therefore produced **zero** signal.
  Fixed by `trackFilter()`, called from
  [frontend/src/pages/Shop.jsx](frontend/src/pages/Shop.jsx) on the active
  category/brand: the facet is emitted as a `search` event with
  `meta.source = "filter"`. Reusing `search` rather than adding an event type
  means it inherits weight 3.0, `MEANINGFUL_TYPES` membership, and
  `recent_searches` — which the agent turns into retrieval queries
  ([nodes.py:79](app/agent/nodes.py#L79)) — with no change to ingest, scoring or
  the agent. `meta.source` is what lets the "Your Signal" panel label it
  *Filtered* rather than claiming the user typed a query. A `sessionStorage`
  guard suppresses an identical facet within 30s so a reload or a pagination
  click can't inflate `events_since_last_rec`. The typed `q` is deliberately
  excluded — the search box fires its own event, and including it here would
  count one search twice.
  **Known bound:** `has_signal()` requires a non-empty `category_affinity`, and
  only events carrying a `product_id` populate that. So filtering alone still
  won't cold-start a *first* recommendation — one product must be opened.
  Filtering does move momentum, `high_signal`, tag affinity and retrieval.
  Pinned by `test_a_facet_survives_the_real_ingest_path` (through the real
  `_write_batch`) and `test_filtering_alone_still_cannot_cold_start`.

### The agent
- LangGraph `StateGraph` in [app/agent/graph.py](app/agent/graph.py):
  `analyze_behavior → decide_retrieval →(skip) cold_start | multi_query_retrieve
  → grade_relevance →(weak, max 2) refine_query | rerank_and_select →
  generate_narrative → validate_grounding`.
- Multi-query retrieval (3–4 distinct queries) + **reciprocal rank fusion**
  (`RRF_K = 60`) + metadata filters (published, brand, price band).
- Reranking is **multiplicative**: topical fit (similarity, RRF, multi-query
  agreement, category affinity) is the only thing that makes an item eligible;
  novelty / brand fit / rating / price fit modulate it. Similarity is normalised
  against the candidate set, so ranking is invariant to embedding-provider scale.
- `validate_grounding` drops any product id that wasn't retrieved and flags a
  narrative quoting a product we don't stock. `_persist` re-checks every id
  against the products table before writing.
- **`brand` is the metadata facet the agent filters on**, and unlike the closed
  three-value `level` enum it replaced, it is an *open* vocabulary that grows with
  every product an admin adds. Two silent failure modes come with that, and both
  end in a thin or empty slate rather than an error: a brand we don't stock builds
  a perfectly valid Chroma filter matching nothing, and a brand we *do* stock may
  have nothing in the topical area the queries describe. So `analyze_behavior` is
  given the stocked vocabulary (`catalog.brands()`) and validates the model's
  suggestion against it, and `multi_query_retrieve` retries unfiltered when a
  brand filter returns zero hits — clearing `metadata_filter` on the way out so
  the reranker stops handing a just-disproved brand its tie-break bonus. Pinned
  by [tests/test_brand_facet.py](tests/test_brand_facet.py).
- **Verified end to end on two personas** after the Gemini swap, with real
  vectors and a real chat model. `runner`: 40 events, 7 on-persona products,
  path `analyze_behavior → multi_query_retrieve → rerank_and_select →
  generate_narrative → validate_grounding`, `model=gemini-3.6-flash
  fallback=False`. The model wrote its own retrieval queries
  (`"responsive tempo road running shoes with 6mm drop"`, `"cushioned post-run
  recovery slides"`, `"moisture wicking anti-blister performance running
  socks"` — note the third is an *unmet need*, nothing the user searched for)
  and its own copy, with a distinct grounded reason per item. `audiophile` on
  the same catalog returned four Electronics & Audio items, four different
  queries and different copy — which is the demo that proves behaviour drives
  the output.
- The narrative's factual claims were spot-checked, not trusted: it said "these
  options from Kestrelane", and all four recommended products really are
  Kestrelane. `validate_grounding` guarantees the *ids*; brand claims in prose
  are not machine-checked, so that one was verified by hand.

### Efficiency / when the model runs
- [app/services/triggers.py](app/services/triggers.py) — no AI calls, only
  counters, timestamps and hashes. Four gates: single-flight lease (a
  conditional `UPDATE`, not read-then-write, stale reclaimed after 2 min),
  insufficient signal, cooldown (10 min), momentum (8 events OR one high-signal
  action OR expired+activity), and **novelty** — a behaviour signature (SHA256
  of decayed top categories + searches + engaged products) that must differ from
  the one that produced the current rec.
- Page loads read a cached row and dispatch regeneration to a background task
  (stale-while-revalidate). Background tasks are held in a module-level set,
  because asyncio only keeps a weak reference and a task nobody holds can be
  garbage-collected mid-await.
- `/admin/ai-usage` reports calls by purpose, tokens, latency, and the headline
  **events-per-LLM-call** ratio.
- **Gate order matters, and it surprises people demoing it.** Cooldown is checked
  *before* momentum and novelty ([triggers.py:97-102](app/services/triggers.py#L97-L102)),
  so once a recommendation exists nothing regenerates for 10 minutes — not even
  an `add_to_cart`, because `high_signal` is only consulted later at line 115.
  That is deliberate (it is the spend ceiling), but it means the honest answer to
  "how fast does it react?" is: **10 min cooldown + 2 meaningful events + ~40s of
  generation**, and during the window `/api/recommendations/refresh` returns
  `queued: false` — the *Refresh* button on **For you** then shows the gate's own
  `decision` string rather than an error, because "not enough new signal" is a
  real answer.
  For a live demo set `REC_COOLDOWN_MINUTES=1` in `.env` (annotated in
  `.env.example`) and restart; the committed default stays 10.

### Bonus 1 — LangGraph
The workflow above, including the genuine cold-start branch and the bounded
refine loop. Full node path, timings, queries and refine count persist to
`recommendations.agent_trace` and render at `/admin/traces`.

### Bonus 2 — scheduled proactive delivery
- [app/scheduler.py](app/scheduler.py), APScheduler `AsyncIOScheduler` started
  in the FastAPI lifespan. Three jobs: daily digest at `DIGEST_HOUR` UTC,
  catalog reconcile every 5 min, hourly housekeeping (expire past-TTL recs,
  trim events past `EVENT_RETENTION_DAYS`).
- Digest goes to users with ≥3 events in the last 24h. The `digest_log` row is
  **claimed before the send**, so the unique `(user_id, digest_date)` index
  makes a double run a no-op instead of two emails.
- [app/services/mailer.py](app/services/mailer.py): `outbox` backend (default)
  writes real rendered HTML to `outbox/` and serves it at `/inbox`; `smtp`
  sends the identical message via aiosmtplib. `send()` returns `(ok, detail)`
  and never raises, so one failed digest can't abort the run for other users.
- **Verified end to end:** first run `sent=1`, second run `already_sent=1`,
  one `digest_log` row, a 7,858-byte HTML digest in `outbox/` with working
  `/product/{id}` links, viewable at `/inbox`. Outbox ownership checks pass
  (own message reads; another user's returns `None`; `../app/config.py`
  returns `None`).

### Bonus 3 — LangSmith observability
- [app/ai/tracing.py](app/ai/tracing.py). Env-gated on `LANGSMITH_TRACING` +
  `LANGSMITH_API_KEY`. `traceable()` returns the **undecorated function** when
  tracing is off, so there is no wrapper and no per-call cost without a key.
- `gemini_client.chat` and `.embed` carry spans; the input formatter drops `self`
  so the API key can't reach the trace payload.
- The local `/admin/traces` viewer works regardless, so the workflow is always
  inspectable.
- **Not yet exercised against a live LangSmith account** — see §2.

### Bonus 4 — retrieval polish
Multi-query expansion, RRF fusion, metadata filtering, candidate-set-normalised
scoring, multiplicative reranking, per-category cap (`MAX_PER_CATEGORY = 2`),
below-band filler capped at `MIN_SLATE = 2` so a weak pool doesn't pad the slate
with irrelevance.

### Tests — 169 passing, no network required
```
tests/test_storefront_api.py          32  catalog filters/sort/paging, facets, 404 on unpublished
tests/test_gemini_provider.py         27  message translation, schemas, thinking, width
tests/test_json_auth.py               30  401-not-303, JSON auth, HTML/JSON rule parity,
                                          form redirect targets + open-redirect guard (§5)
tests/test_signal.py                  19  feed wording, cross-user isolation, limit clamp
tests/test_triggers.py                17  the four gates, lease contention, force semantics
tests/test_profile.py                 14  decay maths + signature stability + facet intent
tests/test_grounding.py               10  hallucinated ids and titles
tests/test_rerank.py                   9  reranker weighting (+2 for the brand tie-break)
tests/test_brand_facet.py              9  open-vocabulary brand validation + relaxation
tests/test_event_consumer_shutdown.py  2  the consumer stays cancellable (§5)
```

There is **no JS test suite**. The storefront's logic that could be unit-tested
is mostly the tracker, and the property that matters about it — that a batch
actually arrives, attributed to the right user, with the facet labelled — is a
property of the *pair*, which `test_profile.py`'s facet tests already pin through
the real ingest path. Everything else was verified by driving the running app.

`test_cooldown_blocks_a_rapid_second_generation` now pins
`settings.rec_cooldown_minutes` with `monkeypatch` rather than reading it: the
suite loads the developer's `.env`, which may carry `REC_COOLDOWN_MINUTES=1` for
a live demo, and a 1-minute-old rec would then fall *outside* the window and
reach the momentum gate instead. That was a test-isolation bug, not a domain one.
The Gemini fixture pins `gemini_chat_model` for the same reason — the thinking
assertions depend on which model generation is configured (§5).

`test_event_consumer_shutdown.py` runs each scenario on a hand-made event loop
that is closed rather than drained, and waits with `asyncio.wait` instead of
`asyncio.wait_for`. Both are deliberate. A task that ignores cancellation also
blocks `asyncio.run`'s teardown, and `Task.cancel()` on a task parked on
`await other_task` forwards the cancel to `other_task` rather than interrupting
the waiter — so the obvious spelling of this test *hangs the suite* when the bug
is present instead of failing. It was verified both ways: re-introducing the bug
now produces a red test in 6s.

`test_json_auth.py` is the only suite that goes through `TestClient`, and it
needs `StaticPool` where the others use a plain `create_engine("sqlite://")`.
TestClient runs the app on a worker thread and the default pool for in-memory
SQLite hands out a connection *per thread*, so the request would otherwise land
on a second, empty database. It also pins `settings.debug = True` rather than
inheriting it: `set_session_cookie` sets `secure=not settings.debug`, and a
Secure cookie is never sent back over `http://testserver`, so with `DEBUG=false`
in the environment every signed-in assertion would fail for a reason unrelated to
the code. Verified both ways here too — reverting `api_user` to a 303 turns 5 of
the 30 red.

`test_storefront_api.py` is the newest suite and covers the JSON catalog the
React app reads: that the filter set narrows the *count* and the page together
(two hand-maintained WHERE clauses would eventually paginate against the wrong
total), that an unknown `sort` falls back rather than 500s, that every sort key
`/api/catalog/facets` advertises actually works, that `page_size` and an
out-of-range `page` are clamped rather than rejected, that no match is an empty
page rather than a 404 (the empty state is a design element on the client), and
that an unpublished product 404s instead of 403s so its existence isn't probeable.

It also pins the **CORS contract**, which is otherwise invisible from the server
and only fails in a browser: the dev origin is echoed back with
`Access-Control-Allow-Credentials`, the allowed origin is never `*`, an unlisted
origin gets no CORS headers at all, and the preflight for a JSON `POST` is
answered — that last one is what the tracker's non-beacon flush depends on.

### Git
`.env`, `venv/`, `data/`, `outbox/`, `*.db`, `chroma/` are all excluded — the
staged list was checked explicitly, not just trusted to `.gitignore`.

---

## 2 · Pending / open

### (a) The Gemini swap, and the trap it uncovered

Mesh is gone. `app/ai/mesh_client.py` is deleted and
[app/ai/gemini_client.py](app/ai/gemini_client.py) is a native `google-genai`
integration, not a base-URL shim — Gemini has no `system` role, takes a
`response_schema` instead of a JSON-mode promise, and bills hidden thinking
tokens, and all three differences are handled rather than papered over. The
previous limitation (no account balance, so embeddings *and* chat 402'd and the
copy was mechanical) is resolved: retrieval runs on real
`gemini-embedding-001@768` vectors and the narrative is model-authored.

**The thing that would have bitten silently.** The plan asked whether
`reconcile()` handles a *different vector dimension*, not just a different
provider id. It did not, and the failure would have been ugly:

- Chroma pins a collection's dimensionality **at first insert** (verified
  empirically against chromadb 1.5.9 — both `upsert` *and* `query` raise
  `InvalidArgumentError` on a mismatch; only `delete_collection` + recreate
  accepts a new width).
- `sync_product_vector`'s bare `except Exception` would have caught every one of
  those and marked all 56 rows `vector_state='failed'`, permanently.
- `store.query()`'s "retry unfiltered" path was unprotected, so retrieval would
  have raised rather than degraded — the mismatch raises whatever the filter is.

Three coordinated fixes, all in Step 2: `store.dimension()` reports the pinned
width; `catalog._ensure_dimension()` runs *first* in `reconcile()` and resets the
collection when it disagrees with `embeddings.target_dim()`; and the local
fallback is now dimension-parametric, so a mid-run Gemini failure yields
same-width vectors instead of poisoning the collection. The query retry now
returns `[]` instead of re-raising.

**This also retroactively corrects a claim these notes used to make.** The old
§2(a) said switching embedding providers needed "No code change", because
`reconcile()` compares the stored `embed_provider` metadata and re-embeds. That
was **false at the time** — `openai/text-embedding-3-small` is 1536-dim against a
512-dim fallback, so topping up the Mesh account would have jammed every row at
`failed`. It never surfaced only because Mesh embeddings 402'd from the very
first call, so the width never actually changed. The claim is **true now**, but
for a different reason than it originally gave: reconcile resets on a width
change. Live proof, watched rather than reasoned about — the 512→768 switch
reported `dimension_reset={'from': 512, 'to': 768}`, `reembedded=56`,
`still_failing=0`, and left `vector_providers=['gemini:gemini-embedding-001@768']`.

Note `resolve_provider()` in embeddings.py: it does one probe embed before
comparing, because `provider_id()` alone was a *guess*. Without the probe, a
cold start on a Gemini-unavailable deployment marked all 56 products
provider-stale and re-embedded the whole catalog every time. Don't remove it.
Relatedly, `target_dim()` is derived from **configuration, not runtime health** —
it must answer "how wide should this collection be", which a failing provider
doesn't change.

### (b) Everything else not yet closed

- **Step 3 is done** — see the table at the top. The customer pages are React on
  `:5173`, the API is JSON on `:8000`, `CORSMiddleware` runs with
  `allow_credentials=True` against an explicit `CORS_ORIGINS` list, and `/admin/*`
  plus `/inbox` stay on Jinja. What is *not* done, and was never in scope: there
  is no checkout. The bag is `localStorage` only and the button says so rather
  than leading anyone into a flow that does not exist — the add-to-bag *event* is
  what the recommender actually needs, and it is real.
- **`samesite="lax"` only survives same-site deployment.** In dev, `:5173` and
  `:8000` are the same *site* (the port is not part of a site), so the session
  cookie is sent on cross-origin `fetch` and everything works. Serving the
  storefront from a different registrable domain than the API would silently stop
  it from being sent; that needs `samesite="none"; secure` and a real HTTPS
  origin. Same-domain deployment (a path or subdomain behind one host) avoids the
  question entirely and is the recommended shape. `vite.config.js` sets
  `strictPort: true` for the neighbouring reason: if Vite quietly moved to :5174,
  that origin is not in `CORS_ORIGINS` and the symptom would be "mysteriously
  signed out" rather than "failed to boot".
- **The storefront build is not wired into any deployment.** `npm run build`
  produces `frontend/dist/` (~321 kB JS, ~99 kB gzipped; 36 kB CSS) and nothing
  serves it — dev runs Vite, and `STOREFRONT_URL` is what emails and the API index
  point at. Putting `dist/` behind the same host as the API is the deployment that
  makes the cookie question above disappear.
- **Steps 1–3 are on disk but uncommitted.** 169 tests pass and the seed →
  simulate → recommend walkthrough was run end to end on two personas.
- **`data/app.db` was recreated, not migrated.** `init_db()` uses
  `create_all()`, which cannot add the `brand` / `size` / `color` columns to an
  existing `products` table — the old database failed with
  `no such column: products.brand`. The pre-swap database and Chroma directory
  are kept as `data/app.db.learnloop.bak` and `data/chroma.learnloop.bak` (both
  gitignored); they hold the old course rows and two real accounts. Delete them
  once you're sure nothing there is wanted.
- **LangSmith is wired but never run against a live account.** The code path is
  exercised in the off state (which is the default and degrades silently). If a
  key gets added, check that spans actually arrive and that nothing in the
  payload carries the Gemini key.
- **SMTP backend is untested against a real server.** `outbox` is the default
  and is fully verified; `_send_smtp` is straightforward aiosmtplib but has
  never had a live SMTP host pointed at it.
- **Nothing runs the tests automatically.** `pytest` is clean locally, and
  nothing lints or builds the frontend either. The one workflow in the repo,
  `.github/workflows/smartreco-build-challenge-2026-checks.yml`, is the
  hackathon platform's own submission check — it performs an OIDC handshake
  against the organiser's API and runs no part of this suite. **It is currently
  deleted in the working tree and that deletion is not committed**, so a push
  from here would remove it from the repo; restore it with
  `git checkout -- .github/` unless the removal is intended.
- **Digest email rendering only checked in a browser-agnostic way** — the
  template is table-based with inline styles (correct for email clients), but it
  hasn't been through Litmus or a real Gmail/Outlook render. Its customer-facing
  links point at `STOREFRONT_URL` now, so a digest sent with that misconfigured
  would land on a dead origin.

Two pieces of conversion debris have been cleared since: `app/static/css/app.css`
carried the storefront's old rules (product cards, the recommendation layout, the
cart chip, toasts, the signal panel) that nothing loads any more — 313 lines down
to 164, cross-checked class-by-class against every remaining template rather than
eyeballed, and all eleven server-rendered pages re-probed afterwards. And the ten
`<div data-ll-page="…">` hooks in those templates are gone; they existed only to
be scraped by the vanilla tracker, which is deleted, so they were markup that
looked load-bearing and wasn't.

Nothing here blocks the submission.

---

## 3 · Running it from a clean terminal

Windows, Git Bash or PowerShell. Python 3.13.5 is what's installed here;
3.11+ works. Node 20+ for the storefront.

**Two processes now.** The API and the store are separate origins — one terminal
each, and the store is the one you open.

```bash
cd d:/Hackathon

# 1. Virtual environment (already exists as venv/ — recreate only if missing)
python -m venv venv
venv/Scripts/activate            # Git Bash
# venv\Scripts\activate          # PowerShell / cmd
pip install -r requirements.txt

# 2. Config — .env already exists locally with the Gemini key.
#    On a fresh clone:
cp .env.example .env             # copy on Windows cmd
#    then put GEMINI_API_KEY in .env.  NEVER commit .env.
#    Key from https://aistudio.google.com/apikey.

# 3. Seed 56 products + demo accounts (dual-writes to SQL and Chroma)
python -m scripts.seed

# 4. Terminal A — the API on :8000
uvicorn app.main:app --reload

# 5. Terminal B — the storefront on :5173
cd frontend
npm install
npm run dev
```

Then **http://localhost:5173** for the store. The API origin is
http://localhost:8000 — that is where `/admin`, `/inbox` and `/docs` live, and
`/` there is a signpost, not a catalog.

The frontend needs no `.env` for local dev: `VITE_API_URL` defaults to
`http://localhost:8000`, which is what uvicorn serves. Point it elsewhere and
that origin must also appear in the API's `CORS_ORIGINS` —
[frontend/.env.example](frontend/.env.example) says so, and no trailing slash.
Two settings on the API side matter for the split: `STOREFRONT_URL` (where emails
and the API index send a shopper) and `CORS_ORIGINS` (comma-separated, explicit,
never `*`).

| Account | Password | For |
|---|---|---|
| `admin@bazario.dev` | `admin123` | product CRUD + all admin views |
| `shopper@bazario.dev` | `shopper123` | the storefront and **For you** |

Signing in through the *Jinja* form at `:8000/login` sends an admin to `/admin`
and anyone else to `STOREFRONT_URL`; shoppers normally sign in on the React page
at `:5173/login` instead, which is the same session cookie either way.

**Without installing anything through the venv, nothing works** — every install
in this project went through `venv/Scripts/python -m pip`, never a global pip.

### Demoing the recommender without clicking around

```bash
python -m scripts.seed --simulate-user runner --recommend
python -m scripts.seed --simulate-user audiophile --recommend
```

Four personas: `runner`, `home-cook`, `audiophile`, `skincare`. Each replays a
realistic shopping session (searches, views, dwell times, a cart add), then runs
the agent. Sign in as the shopper and open **For you**. The personas produce
visibly different slates — that's the demo that proves behaviour drives the
output. Each seeds `random` with a fixed string, so a persona replays
identically every time.

### Why the seed backdates the catalog

`scripts.seed` ends with `backdate_catalog()`, which spreads the seeded products'
`created_at` across `CATALOG_WINDOW_DAYS = 60`, interleaved by category. It looks
like set dressing and isn't. The seed writes all 56 rows within a second or two,
so `sort=newest` had nothing to order by and collapsed into reverse insert order
— which meant the homepage's **New arrivals** strip showed four items from
whichever category happens to sit last in `seed/products.json`, and the catalog's
newest sort silently ranked by nothing. Interleaving by category makes both read
like a real shop's arrival order.

It matches on seed-file titles only, so a product an admin creates through
`/admin/products/new` keeps its real timestamp and appears where it should.

### Tests

```bash
venv/Scripts/python -m pytest tests/ -q      # 169 passed, ~20s, no network
```

### Demoing the scheduler without waiting for 16:00 UTC

`/admin/scheduler` → **Run digests**. Same function the cron trigger calls.
Press it twice: the second press is a no-op, which is the `(user_id,
digest_date)` uniqueness doing its job. The digest appears in `outbox/` and at
`/inbox`.

---

## 4 · Known issues / watch

- **2.5-era Gemini models 404 for new keys.** `gemini-2.5-flash` was the
  committed default and the API refuses it outright: *"no longer available to new
  users. Please update your code to use models/gemini-3.6-flash."* The default is
  now `gemini-3.6-flash`. Worth knowing because the failure is a **404, which is
  in `PERMANENT_FAILURES`** — so the app didn't crash, it silently degraded to
  behavioural analysis and mechanical copy exactly as designed. That is the
  fallback working, and it is also why a dead model can hide behind a
  still-functioning app. If the narrative ever goes mechanical, check
  `/admin/ai-usage` for failed rows before assuming a logic bug.
- **The slate spread again once real embeddings landed.** The old note here said
  the `runner` walkthrough returned four Footwear items, exceeding
  `MAX_PER_CATEGORY = 2`, because the local hash embedder compresses the
  similarity range so too few categories clear the relevance band and the ladder
  concedes variety before relevance (`test_cap_is_relaxed_before_relevance_is`).
  That prediction held: with Gemini vectors `audiophile` returns a properly
  varied Electronics & Audio slate. `runner` still concentrates in Footwear, but
  now legitimately — the model's own queries were all running-related, including
  an unmet-need query for socks.
- **`venv/` is 400MB+ and gitignored.** Don't commit it. If the repo ever looks
  suspiciously large, that's the first thing to check.
- **`.env` contains a real Gemini key, and it should be rotated.** It is
  gitignored from the first commit and is in no tracked file and no commit
  (verified with `git grep` and `git log --all -S`). But it was briefly pasted
  into `.env.example` — a *tracked* file — before being moved, and it appeared in
  plaintext in a terminal session. Nothing leaked, and rotating at
  https://aistudio.google.com/apikey is still the cheap safe call. Re-verify with
  `git diff --cached --name-only` before any future push.
- **`data/app.db` and `data/chroma/` are local state, also gitignored.** A fresh
  clone must run `python -m scripts.seed` or the catalog is empty and the
  recommender has nothing to retrieve.
- **A fresh clone also needs `npm install` in `frontend/`.** `node_modules/` is
  gitignored, and `npm run dev` on a clean checkout fails with a missing-vite
  error that reads like a broken project rather than a missing step.
- **There are no product photographs, and the storefront doesn't pretend
  otherwise.** Every product panel is a generated duotone composition, derived
  deterministically from the product id with its hue keyed to the category. It is
  a considered visual choice rather than a placeholder — but it does mean a
  related-products strip shows four panels in one hue, which looks like a
  rendering bug until you know it's the category showing through. A real image
  column would drop in without a schema change; nothing keys off the panels.
- **APScheduler is in-process.** Correct for a single-node hackathon build. With
  multiple uvicorn workers each would start its own scheduler; the `digest_log`
  claim makes duplicate *emails* impossible, but the README's note about Celery
  Beat is the honest answer for a real deploy.
- **`DIGEST_HOUR` is UTC, not local time.** Set deliberately; just don't be
  surprised when the job fires at a different wall-clock hour.
- **Line endings**: git warns `LF will be replaced by CRLF` on every add. Cosmetic
  on Windows, safe to ignore.

---

## 5 · Key decisions, and why

**FastAPI over Flask.** Event ingest has to return *before* it does any database
work, which wants native async plus a background consumer task. Flask would have
meant a thread pool or an external queue for the same result.

**Gemini embeddings primary, local deterministic fallback.** All AI traffic goes
through one module, so the provider contract lives in exactly one place. But an
embedding model's availability is independent of a chat model's, and a submission
that dies because one endpoint 403s demonstrates nothing. The fallback keeps
retrieval working — lexical-ish rather than semantic — and because `reconcile()`
detects both the provider change *and* the width change, going from fallback to
real vectors is a restart, not a rewrite.

**Chroma with app-supplied embeddings**, not Chroma's built-in embedding
function. If Chroma embedded internally it would pull its own model and AI
traffic would leave the single-module path, which is the constraint the whole
`app/ai/` boundary exists to enforce.

**Dev outbox as the default digest backend.** The scheduler bonus has to be
demonstrable by whoever clones this, and requiring SMTP credentials would mean
it isn't. `outbox` writes the *same* rendered HTML the SMTP path would send, so
the feature is real rather than mocked; `EMAIL_BACKEND=smtp` goes live with no
code change.

**The digest does not force generation.** First pass used `force=True`, reasoning
that a scheduled send shouldn't be blocked by an interactive cooldown. That was
wrong: it would spend an LLM call to reproduce identical copy whenever the day's
signature was unchanged, which is exactly what the novelty gate exists to
prevent. It now runs the normal gates and forces only when there is no
recommendation at all to send.

**Reranking is multiplicative, not additive.** With additive weights the hygiene
terms (novelty + brand fit + rating + price fit) summed to more than category
affinity, so a tidy-metadata product could outrank a relevant one — a shopper
browsing running shoes was being shown a ceramic table lamp. Topical fit is now
the only thing that makes an item eligible; hygiene factors can only re-order
what's already relevant. `tests/test_rerank.py` pins this, and after the swap two
tests pin the same property for `brand` specifically: its cardinality is far
higher than the enum it replaced, so the temptation to treat a brand match as a
qualifier rather than a tie-break is correspondingly larger.

**Similarity normalised against the candidate set.** Different embedding
providers return different score scales. Normalising per-query means the ranking
is identical whether the vectors came from Gemini or the local fallback.

**Behaviour signature as the cache key.** The single highest-leverage efficiency
decision. Identical behaviour would produce identical copy, so an unchanged
signature serves the stored recommendation and spends nothing — however much new
activity there is. It's checked *before* the momentum gate, deliberately;
momentum alone would approve a reload-hammering tab.

**Ingest reads identity from the cookie, not the database.** `optional_user_id`
skips the connection checkout and SELECT that `optional_user` would do on the
busiest endpoint in the app. The cookie is signed, so the id is exactly as
trustworthy as it would be after a lookup.

**Grounding as a node, not a prompt instruction.** "Please only recommend real
products" is a hope. A node that drops every id absent from the retrieved set,
plus an id re-check against the products table before persistence, is a
guarantee.

**Outbox filenames are `recipient__timestamp__subject`.** The double underscore
matters: the outbox is a shared directory, and `list_outbox`/`read_outbox` filter
by recipient so one user can't read another's digest at `/inbox/{filename}`.
Single dashes appear inside all three fields, so the address wasn't recoverable
before this change.

**bcrypt directly, no passlib.** passlib's bcrypt backend is broken against
bcrypt 5.x. Calling bcrypt directly is three lines and has no version coupling.

**Vector width is part of the provider identity.** `embed_provider` metadata
records `gemini:gemini-embedding-001@768`, not just the model — the `@768` is
load-bearing. Chroma pins a collection's width at first insert, so the width is
not a tuning knob you can change and re-embed past; it requires dropping the
collection. Folding it into the provider string means the existing
provider-change detection catches a dimension change for free, and
`_ensure_dimension()` only has to decide *reset or not*. `target_dim()` is
deliberately derived from configuration rather than runtime health: "how wide
should this collection be" is not a question a failing API call gets to answer.

**Retrieval queries are embedded as queries, not documents.** Gemini builds an
asymmetric pair — `RETRIEVAL_QUERY` vectors are optimised to land near the
`RETRIEVAL_DOCUMENT` vectors the catalog stored. Getting this backwards costs
relevance and raises nothing, so `task_type` is also part of the embed cache key;
otherwise the first caller for a given string would decide the task type for
every later one.

**Structured output via `response_schema`, not a JSON-mode promise.** Gemini
constrains decoding to the schema, so "the model returned prose where JSON was
expected" stops being a failure mode rather than being handled. The schemas live
in `prompts.py` next to the prompts they pair with, as plain dicts — the module
stays importable without the SDK, and the pairing is visible in one file.

**Minimal thinking on JSON calls, full thinking on the narrative.** Gemini spends
hidden reasoning tokens before answering, billed as output but excluded from
`candidates_token_count`, so the ledger adds them back or it under-reports the
invoice. A schema already supplies the structure that thinking would derive, so
JSON calls ask for the floor; the narrative is the one place it earns its cost.
The two generations spell "as little as possible" differently — 2.5 takes
`thinking_budget=0`, 3.x removed that in favour of `thinking_level="MINIMAL"` and
rejects the numeric form with a 400 — and since a model id like
`gemini-flash-latest` names no generation at all, the client guesses from the
name and treats a 400 as *the wrong guess*, retrying once with the other
spelling. That beats pinning one spelling: this default has already been
retired once.

**A dead default model taught the fallback's real lesson.** `gemini-2.5-flash`
started 404-ing for new keys, and because 404 is a permanent failure the app
degraded silently to mechanical copy instead of erroring. The degradation is
correct and was the point — but it means "the site works" is not evidence the
model does. Hence the failure column on `/admin/ai-usage` and the `model` /
`fallback` line the seed walkthrough prints.

**The event consumer must stay cancellable.** `_drain_once` caught
`(asyncio.TimeoutError, asyncio.CancelledError)` together and returned 0 for
both. The timeout half is right — an idle queue is not an error — but swallowing
the cancellation made the consumer task refuse to stop: `_consumer` looped
straight into the next drain, its own `except CancelledError: break` never ran,
and `stop_consumer`'s `await _consumer_task` waited forever. Nothing raised and
nothing logged; the only symptom was that shutdown never finished, so `uvicorn`
looked wedged and Ctrl-C looked ignored.

This is a pre-existing bug, not a Gemini one, and it was found only because
verifying "the app still boots and serves" meant driving a real lifespan. It
survived this long because `TestClient(app)` outside a `with` block never runs
lifespan at all — the older note that "all routes were verified under TestClient"
was true and still missed it entirely. Anything that only runs on startup or
shutdown needs the context-manager form to be exercised at all.

Two things about the fix are worth keeping in mind if it is ever revisited: a
task that ignores cancellation blocks `asyncio.run`'s teardown just as it blocks
`stop_consumer`, and `Task.cancel()` on a task parked on `await other_task`
forwards the cancel to `other_task` rather than interrupting the waiter. Together
those mean the natural regression test hangs instead of failing — which is why
the test file builds its own loop and waits with `asyncio.wait`.

**A second auth dependency, `api_user`, rather than changing `current_user`.**
`current_user` raises a 303 to `/login`, which is the right answer for a browser
navigation and the wrong one for a JSON endpoint: `fetch` follows a redirect
transparently and without opting in, so a React client asking for
`/api/recommendations` while signed out receives the login page's HTML under a
`200`. That fails as a JSON parse error somewhere unrelated, or silently if the
caller only checks `res.ok`. The three endpoints in `recs.py` now depend on
`api_user`, which raises a plain `401` the client can branch on.

The `/api/`-path guard in `main.py`'s exception handler is *not* sufficient on
its own, and this was confirmed by measurement rather than assumed: it swaps the
body for JSON but preserves the 303 status and the `Location` header, so the
client still follows the redirect. It stays as a backstop for anything under
`/api/` that raises a redirecting exception in future, but the fix is the
dependency. `require_admin` deliberately still routes through `current_user` —
`/admin/*` is Jinja and wants the redirect.

**One set of registration rules behind two front doors.** `auth.py` now serves
both HTML forms and `/api/auth/*` JSON. `_registration_error`, `_create_user`,
and `_authenticate` are shared rather than duplicated, because two copies of a
validation rule drift and the looser copy is the one that ends up mattering.
`tests/test_json_auth.py` drives both paths with the same input and compares the
messages, including their order, instead of restating the strings — a test that
hard-coded them would still pass if the paths disagreed.

**`GET /api/auth/me` exists, and returns `{"user": null}` rather than 401.** The
three endpoints originally scoped (login/register/logout) cannot answer the
question a header actually asks on a cold page load, which is "is there already a
session?". Anonymous is a normal state there, not an error: answering 401 would
put a red line in the console on every anonymous visit and push the client into
treating a valid state as a failure. `POST /api/auth/logout` is idempotent for
the same reason — a client whose cookie has already expired is trying to reach
exactly the state that endpoint produces.

**JSON login issues the same signed cookie, and takes no `next`.** There is no
JWT and no second mechanism; a JSON login is the same login, and `httponly`
means the React app can never read the session out of `document.cookie`. The
`next` parameter is form-only on purpose: where to go after signing in is the
client's routing decision, and accepting a redirect target over JSON would
recreate the open-redirect risk the form handler guards against for no benefit.

**`PublicUser` is built field by field, not from the ORM object.** Serialising
`User` directly would mean any column added later joins the response by default —
and `password_hash` is a column on that model. Five explicit fields, with a test
pinning the exact key set.

**Two origins, one cookie.** The storefront is a Vite app on `:5173` and the API
is uvicorn on `:8000`, which makes every shopper request cross-origin. The
alternative — building the React app and serving `dist/` from FastAPI — would
have kept one origin, but it also means no HMR and a rebuild between every visual
change, on a build whose whole third step is visual work.

Three things follow from the split, and each is a place it could have quietly
broken:

- `CORSMiddleware` runs with `allow_credentials=True`, which makes
  `allow_origins=["*"]` illegal — the browser rejects a wildcard on a credentialed
  request, and it would be the wrong thing to ask for anyway. Hence
  `CORS_ORIGINS` as an explicit comma-separated list. It is comma-separated rather
  than a `list[str]` because pydantic-settings parses list-typed env vars as JSON,
  so `CORS_ORIGINS=http://localhost:5173` would fail to load.
- Every fetch sends `credentials: "include"`, or the session cookie simply is not
  attached and the API sees an anonymous caller with no error anywhere.
- `samesite="lax"` still works, which is easy to doubt: `:5173` and `:8000` differ
  by *port*, and SameSite is a same-**site** rule, so a different port is the same
  site. This is why `vite.config.js` sets `strictPort: true` — Vite's default is to
  hop to the next free port, and a silent hop to `:5174` would leave the origin
  out of `CORS_ORIGINS` and break sign-in in a way that looks like an auth bug.

**There is deliberately no `/api/dashboard`.** The obvious conversion of the old
`/dashboard` template is one endpoint returning the recommendation slate plus the
signal feed plus the user, and it would have been the wrong shape: the React page
wants the two halves independently, so they can arrive independently and each
carry its own skeleton and its own error state. A composite endpoint forces the
slower half to gate the faster one and turns two recoverable failures into one
dead page. `/api/recommendations` and `/api/me/signal` stay separate.

**A redirect target that names a route is a route that can be deleted.** The Jinja
form handlers defaulted to `/dashboard`, Step 3 removed that template, and signing
in through `:8000/login` began 303-ing onto a 404 — with a green test suite, since
nothing asserted the target resolved. `_form_login_target()` now decides by role
(`/admin` for an admin, `STOREFRONT_URL` for anyone else), and the regression test
asserts the destination *serves* rather than restating the path, because a
hard-coded string is exactly what rotted. `/logout` had the same defect in a
quieter form: it redirected to `/`, which Step 3 turned into a JSON signpost, so
signing out of the admin panel dropped a JSON blob in the browser.

**The unload flush posts `text/plain`.** `navigator.sendBeacon` with a JSON
`Content-Type` is not a CORS-safelisted request, so cross-origin it needs a
preflight — and there is no preflight during unload, which is the one moment the
flush exists for. Sending the same JSON body as a `text/plain` Blob is safelisted
and goes straight out. `POST /api/events/beacon` therefore parses the body
without trusting the content-type, and always answers 2xx: a beacon response is
unreadable by definition, so a non-2xx would teach the client nothing and cost a
console error on every page the shopper leaves.
