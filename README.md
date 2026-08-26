# Bazario Mart — an agentic product recommendation platform

An online store whose real product is the **recommendation agent**: a backend
workflow that watches what each shopper actually does, reasons about what
they're trying to buy, retrieves matching products from a vector store, and
writes persuasive copy grounded in the real catalog — then refreshes itself as
behaviour changes, without firing an LLM call at every click.

Built with FastAPI, and a React + Vite storefront on its own origin. Every LLM
and embedding call goes through the **Gemini API** (`google-genai`), in one file.

---

## What's here

| Requirement | Where it lives |
|---|---|
| Web platform, email/password auth, user + admin roles | [app/security.py](app/security.py), [app/routers/auth.py](app/routers/auth.py) |
| Storefront — catalog, product, cart, **For you** | [frontend/src/](frontend/src/), fed by [app/routers/storefront.py](app/routers/storefront.py) |
| Product management with **dual-write** to SQL + vector DB | [app/services/catalog.py](app/services/catalog.py), [app/vector/store.py](app/vector/store.py) |
| Behavioural tracking, non-blocking on both ends | [frontend/src/lib/tracker.js](frontend/src/lib/tracker.js), [app/services/events.py](app/services/events.py) |
| Agentic recommendation engine (RAG over the catalog) | [app/agent/](app/agent/) |
| Restraint about when the model runs | [app/services/triggers.py](app/services/triggers.py) |
| **Bonus 1** — LangGraph structured workflow | [app/agent/graph.py](app/agent/graph.py) |
| **Bonus 2** — scheduled proactive delivery | [app/scheduler.py](app/scheduler.py), [app/services/mailer.py](app/services/mailer.py) |
| **Bonus 3** — LangSmith observability | [app/ai/tracing.py](app/ai/tracing.py) |
| **Bonus 4** — retrieval polish (multi-query, RRF, filters, reranking) | [app/agent/nodes.py](app/agent/nodes.py) |

All four bonuses are implemented.

---

## Setup

Requires Python 3.11+ (developed on 3.13) and Node 20+.

**Two processes**: the API on `:8000`, the storefront on `:5173`. The storefront
is the one you open.

```bash
python -m venv venv
venv\Scripts\activate            # Windows;  source venv/bin/activate on macOS/Linux
pip install -r requirements.txt

copy .env.example .env           # cp on macOS/Linux
# then put your Gemini key in .env  -- .env is gitignored and must never be committed
# get one at https://aistudio.google.com/apikey

python -m scripts.seed           # 56 products + a demo admin and shopper

uvicorn app.main:app --reload    # terminal A — the API
```

```bash
cd frontend                      # terminal B — the storefront
npm install
npm run dev
```

Open **http://localhost:5173** for the store. The API is at
http://localhost:8000, which is where `/admin`, `/inbox` and `/docs` live — `/`
there is a JSON signpost, not a catalog.

The frontend needs no config for local dev: `VITE_API_URL` defaults to
`http://localhost:8000`. Point it anywhere else and that origin must also be
listed in the API's `CORS_ORIGINS` — see [frontend/.env.example](frontend/.env.example).

| Account | Password | |
|---|---|---|
| `admin@bazario.dev` | `admin123` | product CRUD + all the admin views, at `:8000/admin` |
| `shopper@bazario.dev` | `shopper123` | the storefront and **For you** |

### Seeing the recommender work without browsing by hand

```bash
python -m scripts.seed --simulate-user runner --recommend
python -m scripts.seed --simulate-user audiophile --recommend   # different persona, different slate
```

Four personas ship: `runner`, `home-cook`, `audiophile`, `skincare`. Each replays
a realistic shopping session — searches, product views, dwell times, a cart add —
then runs the agent. Sign in as the shopper and open **For you**.

### One thing worth knowing

The app runs with **no Gemini key at all** — the site stays browsable, retrieval
falls back to a local deterministic embedder, and the recommender degrades to
mechanical copy. Nothing 500s. But the interesting path needs a key.

Two details that are easy to get wrong and silent when you do:

**A model can disappear under you.** `gemini-2.5-flash` is no longer served to
new API keys; it 404s with a pointer to the current model. 404 is a *permanent*
failure, so the client stops asking and the app degrades exactly as designed —
which means a dead model looks like a working site with duller copy. If the
narrative reads mechanical, check `/admin/ai-usage` for failed calls. The
default is now `gemini-3.6-flash`; both generations work.

**Changing the embedding dimension rebuilds the index.** Chroma pins a
collection's width at its first insert — a later write *or query* at another
width raises. `GEMINI_EMBED_DIMENSIONS` therefore isn't a free knob:
`reconcile()` detects the change, drops the collection and re-embeds the catalog
(56 products, ~40s). SQL is untouched; the vectors are a derived index. The same
machinery is what makes going from the local fallback to real Gemini vectors a
restart rather than a migration.

---

## Architecture

```
Storefront :5173            FastAPI :8000                   Background
────────────────            ─────────────                   ──────────
tracker.js ──batched──▶ POST /api/events ──▶ asyncio.Queue ──▶ consumer
  (queue,                 returns 202                         (batched INSERT,
   throttle,              *zero DB work*                       idempotent)
   sendBeacon)                                                      │
                                                                    ▼
GET /api/recommendations ─▶ cached rec (indexed SELECT) ────▶ trigger gates
                        │  answers instantly                  (pure SQL/arith)
                        └── stale-while-revalidate                  │ all pass
                                                                    ▼
                                                            LangGraph agent
                                                             │        │
                                                          Gemini    Chroma
```

React on `:5173` talks JSON to FastAPI on `:8000`, cross-origin with the same
signed session cookie (`credentials: "include"`, an explicit `CORS_ORIGINS`
list — never `*`, which the browser rejects on a credentialed request anyway).
`/admin/*`, `/inbox` and the auth forms stay server-rendered Jinja, because a
product-CRUD panel gains nothing from a client-side router.

Three ideas hold it together:

**Nothing on the request path touches the model.** Event ingest returns `202`
before any database work. A recommendation read returns a cached row.
Regeneration is dispatched to a background task and shows up on the next view.

**Sync is enforced, not assumed.** Every catalog mutation writes SQL *and*
Chroma; a `reconcile()` job proves they agree and repairs them when they don't.

**Grounding is a guard, not a hope.** A dedicated node drops any product id the
model invented before anything is persisted.

### Layout

```
app/
  main.py            lifespan: DB init, event consumer, startup reconcile, scheduler
  config.py          env-driven settings          models.py   8 tables
  security.py        bcrypt + signed cookie
  ai/    gemini_client.py the ONLY module that talks to an LLM provider
         embeddings.py    Gemini-primary with a local deterministic fallback
         tracing.py       LangSmith, env-gated                llm_log.py
  vector/store.py     Chroma, app-supplied embeddings
  services/  catalog.py       dual-write + reconcile
             events.py        queue + batched consumer
             profile.py       time-decayed affinity (no LLM)
             triggers.py      the four spend gates (no LLM)
             recommendations.py  runs the agent, persists, background refresh
             presenters.py    ORM rows → JSON shapes the client renders
             mailer.py        outbox | smtp
  agent/  state.py  nodes.py  graph.py  prompts.py
  routers/  storefront.py  JSON catalog/product/facets for the React app
            recs.py  events.py  auth.py (forms + /api/auth)  admin.py  pages.py
  scheduler.py       APScheduler: digest, reconcile, housekeeping
frontend/
  src/  App.jsx  index.css (the design tokens)
        pages/       Home Shop ProductDetail ForYou Cart Login Register NotFound
        components/  Header Footer ProductCard FilterSidebar Skeletons Empty …
        lib/         tracker.js  api.js  productVisual.js  useResource.js
        state/       AuthContext  CartContext
tests/    169 tests, no network
seed/products.json   56 products   scripts/seed.py
```

---

## 0 · The storefront

React 19 + Vite + Tailwind v4, ~30 files, no component library — the visual
language is set by a token block in
[index.css](frontend/src/index.css) rather than assembled from someone else's
defaults: a warm paper/ink palette with a single terracotta accent, Fraunces for
display and Inter for text, and a 2px card radius that stays sharp instead of
rounding into the usual card look.

Seven routes: home (hero, category grid, new arrivals, promo band), `/shop`
(filter sidebar for category/brand/price/rating, sort control, 12-per-page grid,
pagination), `/product/:id` (visual panel, rating, add-to-bag, spec list,
related strip), `/for-you`, `/cart` (line items, quantity controls, subtotal),
`/login`, `/register`, plus a real 404. Checkout is deliberately out of scope;
the cart is `localStorage` and says so.

**For you is not the catalog with a different heading.** It is a single-column
editorial layout: the agent's narrative up top, then each product with its
per-item reason set beside it as visible annotated text under a terracotta rule —
not a tooltip, because the reason *is* the feature and hiding it behind a hover
concedes the point. Alongside it, a signal panel shows the behaviour the slate
was built from, so the shopper can see the input as well as the output.

Every state is drawn rather than left to the browser: loading is skeletons
matched to the real grid geometry (not a spinner), an empty search and an empty
cart each get their own copy and a way out, a cold-start user gets a friendly
explanation of what to do rather than an error, and the API being down reads as a
sentence instead of a blank page. Mobile reflows — the filter sidebar becomes a
drawer, the nav collapses into a slide-in sheet — rather than shrinking.

All fifteen states were verified in headless Chrome over CDP at 1440px and
430px, which is how three defects that were invisible in the code got found.

---

## 1 · Tracking that can't slow the site down

**Client** ([tracker.js](frontend/src/lib/tracker.js), ~460 lines, no
dependencies — plain module, no React in it):

- A queue flushed on whichever comes first: 15 events, 5s idle, tab hidden, or
  page unload. Clicking around never issues a request per click.
- `navigator.sendBeacon` on unload, `fetch(keepalive)` otherwise — the final
  batch survives the page going away without blocking navigation. The beacon
  sends its JSON as a `text/plain` Blob to `/api/events/beacon`: that is
  CORS-safelisted, and there is no opportunity for a preflight during unload.
- Throttled per event class: scroll depth fires once each at 25/50/75/100%;
  search is debounced 600ms so typing "running shoes" is **one** event, not thirteen;
  dwell stops accruing when the tab is backgrounded.
- Each event carries a client-generated `event_id`, so a retried flush can't
  double-count. A `localStorage` copy replays a batch lost to a crash, and only
  the ids the server confirmed are cleared.
- In a SPA no unload fires between pages, so a route change is an explicit page
  boundary: `startPage()` closes the previous page's dwell before opening the
  next. Page context is *passed in* rather than scraped from `data-` attributes,
  because the component already knows what it is rendering.
- Every entry point is feature-detected and try/caught. **If tracking breaks,
  the site keeps working** — that's the requirement, so failure is silent.

**Server** ([events.py](app/services/events.py)): the handler validates the
batch, pushes to a bounded `asyncio.Queue` and returns `202`. It doesn't even
open a database connection — the user id comes from the signed cookie, not a
lookup, because this is the busiest endpoint in the app. A single background
consumer drains the queue and does one multi-row INSERT per batch, skipping
`event_id`s it already holds. Under flood the queue sheds its *oldest* entries:
recent behaviour is what drives recommendations. Shutdown flushes what's queued.

See it: `/admin/events` shows the live feed, the per-type counts (note how few
`search` rows there are relative to keystrokes), and the queue depth.

## 2 · Dual-write, and proof that it holds

One service owns every catalog mutation, so SQL and Chroma can't drift:

- **create/update** → write the SQL row, compute a `content_hash` over the
  embedded text. If the hash is unchanged, **skip re-embedding entirely** — a
  price edit costs zero embedding calls. Otherwise embed and upsert with
  metadata (`category`, `brand`, `size`, `color`, `price`, `rating`, `tags`) for
  filtered retrieval.
- **delete** → removed from both in one unit.
- **failure** → the SQL write still commits with `vector_state='pending'` and
  the admin's request succeeds; a repair job fixes it rather than 500-ing.

`reconcile()` runs at startup and every 5 minutes: retries pending rows, deletes
orphan vectors, and re-embeds anything whose stored fingerprint disagrees with
SQL — including when the *embedding provider* changed, and including when the
provider's **vector width** changed, which needs the collection dropped and
rebuilt because Chroma pins dimensionality at first insert. That's what makes
the Gemini/local fallback switch safe in both directions.

See it: `/admin/sync` reports both sides, which provider the stored vectors
actually came from, and has a manual reindex button. Delete a vector behind
Chroma's back and watch reconcile restore it.

## 3 · The agent

A LangGraph `StateGraph` with real branching, not a linear chain dressed up:

```
analyze_behavior ─decide_retrieval─▶ (too little signal) ──▶ cold_start ──▶ END
        │
        ▼
multi_query_retrieve ──grade_relevance──┬─(weak, <2 tries)──▶ refine_query ──┐
        ▲                               │                                    │
        └───────────────────────────────┼────────────────────────────────────┘
                                        │ (good)
                                        ▼
                  rerank_and_select ─▶ generate_narrative ─▶ validate_grounding ─▶ END
```

- **analyze_behavior** turns the compact profile snapshot into interests, an
  inferred purchase intent, and unmet-need hypotheses. Decoding is constrained to
  a `response_schema`, so "the model replied with prose" isn't a case that needs
  handling. It is also where a brand lean is proposed — validated against the
  brands actually stocked before it ever becomes a filter, because `brand` is an
  open vocabulary and an invented one builds a valid query that matches nothing.
- **decide_retrieval** is a genuine branch: a user who has done nothing never
  reaches the LLM path at all.
- **multi_query_retrieve** issues 3–4 *distinct* queries (top interest, inferred
  intent, adjacent need) against Chroma with metadata filters, then fuses the
  result sets with **reciprocal rank fusion**. A brand filter that returns
  nothing is retried unfiltered rather than left to starve the slate.
- **grade_relevance** scores the candidate pool; a weak pool routes to
  `refine_query`, capped at 2 attempts so the loop provably terminates.
- **rerank_and_select** blends similarity, RRF rank, multi-query agreement and
  category affinity into a *topical* score, then modulates it multiplicatively
  by novelty, brand fit, rating and price fit. The distinction matters: hygiene
  factors can only re-order things that are already relevant, never promote
  something irrelevant. A per-category cap spreads the slate.
- **generate_narrative** writes copy that names the user's actual behaviour
  ("you compared cushioned trainers and trail shoes, and kept coming back to the
  Longhaul Cushioned Trainer"), plus a per-product reason.
- **validate_grounding** drops any id that wasn't retrieved and flags a
  narrative that quotes a product we don't stock.

The full node path, timings, queries and refine count are stored on the
recommendation and rendered at `/admin/traces` — so the workflow stays
inspectable with or without LangSmith.

**What "grounded" means here:** the model never invents a product. It picks from
a retrieved candidate set, the grounding node prunes what isn't in the slate,
and `_persist` re-checks every id against the products table before writing.

## 4 · When the model actually runs

No user action triggers an LLM call synchronously. Ingest increments a counter;
that's all. Generation must clear **four gates**, all pure SQL and arithmetic:

| Gate | What it stops |
|---|---|
| **Single-flight** | Two tabs fanning out into two generations — a conditional `UPDATE` lease, not read-then-write, with stale leases reclaimed after 2 min |
| **Insufficient signal** | Paying to guess about a user who hasn't done anything |
| **Cooldown** | A hard floor on generations per user per 10 minutes |
| **Momentum** | Regenerating over a trickle: needs 8 meaningful events, *or* one high-signal action (60s dwell, cart add, repeat search), *or* an expired rec plus any activity |
| **Novelty** | The big one. The behaviour **signature** — a hash of decayed top categories, recent searches and engaged products — must differ from the one that produced the current rec. Same behaviour ⇒ same answer ⇒ serve the cache, spend nothing |

Also cached: embeddings keyed by `content_hash`, so re-embedding only happens
when the meaning changed.

See it: `/admin/ai-usage` shows calls by purpose, tokens, latency, and the
headline ratio — **events tracked per LLM call**. A naive design that called the
model on every action would sit near 1.

## 5 · Bonuses

**LangGraph** — the workflow above, including the conditional cold-start branch
and the bounded refine loop.

**Scheduler** — APScheduler in the FastAPI lifespan. Daily at `DIGEST_HOUR`
(UTC), every user with ≥3 events in the last 24h gets a digest: the agent runs
in digest mode, the email renders from
[templates/email/digest.html](app/templates/email/digest.html), and delivery is
recorded. The `digest_log` row is claimed *before* the send, so a double run —
two workers, or a restart at the trigger time — is a no-op rather than two
emails. Plus catalog reconcile every 5 minutes and hourly housekeeping.

`EMAIL_BACKEND=outbox` (default) writes the real rendered HTML to `outbox/` and
serves it at `/inbox`, so the feature demos with **zero credentials**;
`EMAIL_BACKEND=smtp` sends the identical message via aiosmtplib. No code change.
`/admin/scheduler` lists the jobs with their next run times and can trigger the
digest job by hand — same function the cron trigger calls — so it can be
demonstrated without waiting for the clock.

**LangSmith** — enabled by `LANGSMITH_TRACING` + `LANGSMITH_API_KEY`. LangGraph
traces natively; the Gemini client's `chat` and `embed` carry `@traceable` spans
with the API key stripped from the payload. With no key the decorator returns
the function untouched — no spans, no overhead, nothing broken. The local
`/admin/traces` viewer is always there regardless.

**Retrieval polish** — multi-query expansion, RRF fusion, metadata filtering
(published, brand, price band), candidate-set-normalised scoring (so ranking is
invariant to the embedding provider's scale), multiplicative reranking, and a
per-category cap.

---

## Verifying the claims

```bash
pytest            # 169 tests, no network required
```

The tests cover the places a subtle bug would be invisible: the reranker's
weighting, the profile decay maths and signature stability, the trigger gates,
the grounding validator, the provider boundary — message translation, schema
enforcement, and the vector-width handling that would otherwise strand the whole
catalog at `vector_state='failed'` — the event consumer's cancellability, where
getting it wrong produces no error at all, just a process that won't exit, the
JSON catalog the storefront reads (including that its filters narrow the count
and the page together, so paging can't drift from the total), the **CORS
contract**, which fails only in a browser and is invisible from the server, and
the JSON API's contract with a browser client: an endpoint that answers an
anonymous caller with a redirect instead of a `401` hands `fetch` an HTML login
page under a `200`, which surfaces as a parse error somewhere unrelated or not
at all.

There is no JS test suite. The storefront's one piece of testable logic is the
tracker, and the property that matters about it — a batch arrives, attributed to
the right user, with a facet selection labelled as one — is a property of the
client/server *pair*, which the Python facet tests already pin through the real
ingest path. The rest was verified by driving the running app.

By hand:

- **Non-blocking tracking** — DevTools Network: batched `POST /api/events` every
  few seconds, not per click; `202` in single-digit ms; a beacon on tab close;
  one event for a typed search. Unset `GEMINI_API_KEY` entirely and the site
  still browses fine.
- **Dual-write** — add a product in admin, confirm the SQL row and the Chroma
  vector. Edit the title → re-embedded. Edit only the price → **not** re-embedded.
  Delete → gone from both. `/admin/sync` reports the counts.
- **Behaviour drives the output** — run both personas above; the narratives and
  slates diverge, and every recommended id exists in the catalog.
- **Efficiency** — click through 20 products, then reload **For you**
  repeatedly and watch `/admin/ai-usage`: the call count doesn't move, because
  the signature didn't.
- **Scheduler** — `/admin/scheduler` → *Run digests*. An HTML digest lands in
  `outbox/` and at `/inbox`, `digest_log` gains one row, and pressing it again
  adds none.

---

## Notes

- **Secrets**: `.env` is gitignored from the first commit; `.env.example`
  documents every key. No credential is needed to read this code.
- **There are no product photographs**, and the storefront doesn't pretend
  otherwise: each product panel is a duotone composition generated from the
  product id, with its hue keyed to the category. It's a deliberate visual
  choice, and an `image_url` column would drop in without touching anything else.
- SQLite runs in WAL mode so the event writer never blocks page reads. The
  schema is plain SQLAlchemy — `DATABASE_URL` can point at PostgreSQL unchanged.
- APScheduler in-process is right for a single-node hackathon build. For a
  multi-worker deploy the digest job belongs in Celery Beat (or a `--scheduler`
  process running alone); the DB lease in `triggers.py` already handles
  multi-process generation correctly.
- The vector store is behind a small interface, so swapping Chroma for pgvector
  is one file.
- `npm run build` in `frontend/` produces a static `dist/` (~321 kB JS, ~99 kB
  gzipped). Nothing serves it yet — dev runs the two processes above; a real
  deploy would put `dist/` behind a CDN or FastAPI's static mount and set
  `STOREFRONT_URL` / `CORS_ORIGINS` to the real hostnames.
