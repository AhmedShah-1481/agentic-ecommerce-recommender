# LearnLoop — an agentic course recommendation platform

An online learning marketplace whose real product is the **recommendation
agent**: a backend workflow that watches what each learner actually does,
reasons about what they're trying to become, retrieves matching courses from a
vector store, and writes persuasive copy grounded in the real catalog — then
refreshes itself as behaviour changes, without firing an LLM call at every
click.

Built with FastAPI + Jinja2. Every LLM and embedding call goes through the
**Mesh API**, in one file.

---

## What's here

| Requirement | Where it lives |
|---|---|
| Web platform, email/password auth, user + admin roles | [app/security.py](app/security.py), [app/routers/auth.py](app/routers/auth.py) |
| Product management with **dual-write** to SQL + vector DB | [app/services/catalog.py](app/services/catalog.py), [app/vector/store.py](app/vector/store.py) |
| Behavioural tracking, non-blocking on both ends | [app/static/js/tracker.js](app/static/js/tracker.js), [app/services/events.py](app/services/events.py) |
| Agentic recommendation engine (RAG over the catalog) | [app/agent/](app/agent/) |
| Restraint about when the model runs | [app/services/triggers.py](app/services/triggers.py) |
| **Bonus 1** — LangGraph structured workflow | [app/agent/graph.py](app/agent/graph.py) |
| **Bonus 2** — scheduled proactive delivery | [app/scheduler.py](app/scheduler.py), [app/services/mailer.py](app/services/mailer.py) |
| **Bonus 3** — LangSmith observability | [app/ai/tracing.py](app/ai/tracing.py) |
| **Bonus 4** — retrieval polish (multi-query, RRF, filters, reranking) | [app/agent/nodes.py](app/agent/nodes.py) |

All four bonuses are implemented.

---

## Setup

Requires Python 3.11+ (developed on 3.13).

```bash
python -m venv venv
venv\Scripts\activate            # Windows;  source venv/bin/activate on macOS/Linux
pip install -r requirements.txt

copy .env.example .env           # cp on macOS/Linux
# then put your Mesh key in .env  -- .env is gitignored and must never be committed

python -m scripts.seed           # 56 courses + a demo admin and learner
uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000.

| Account | Password | |
|---|---|---|
| `admin@learnloop.dev` | `admin123` | product CRUD + all the admin views |
| `learner@learnloop.dev` | `learner123` | the recommendation dashboard |

### Seeing the recommender work without browsing by hand

```bash
python -m scripts.seed --simulate-user agentic --recommend
python -m scripts.seed --simulate-user data --recommend   # different persona, different slate
```

Each replays a realistic browsing session — searches, product views, dwell times
— then runs the agent. Sign in as the learner and open **For you**.

### One thing worth knowing

The app runs fine with no Mesh key at all (the site stays browsable and the
recommender degrades to mechanical copy), but the interesting path needs one.
Mesh **embeddings** are a separate entitlement from chat: if the embeddings
endpoint returns 401/402/403/404 the app logs it once, disables embeddings for
the process, and falls back to a local deterministic embedder so retrieval keeps
working. Chat still runs on the real model, so the agent is genuinely
LLM-driven either way. Topping the account up switches retrieval to real
semantic vectors with **no code change** — `reconcile()` notices the provider
changed and re-embeds the catalog on the next run.

---

## Architecture

```
Browser                    FastAPI                        Background
───────                    ───────                        ──────────
tracker.js ──batched──▶ POST /api/events ──▶ asyncio.Queue ──▶ consumer
  (queue,                 returns 202                         (batched INSERT,
   throttle,              *zero DB work*                       idempotent)
   sendBeacon)                                                      │
                                                                    ▼
GET /dashboard ──▶ cached rec (indexed SELECT) ────────────▶ trigger gates
                        │  renders instantly                  (pure SQL/arith)
                        └── stale-while-revalidate                  │ all pass
                                                                    ▼
                                                            LangGraph agent
                                                             │        │
                                                        Mesh API   Chroma
```

Three ideas hold it together:

**Nothing on the request path touches the model.** Event ingest returns `202`
before any database work. Dashboard renders read a cached row. Regeneration is
dispatched to a background task and shows up on the next view.

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
  ai/    mesh_client.py   the ONLY module that talks to an LLM provider
         embeddings.py    Mesh-primary with a local deterministic fallback
         tracing.py       LangSmith, env-gated                llm_log.py
  vector/store.py     Chroma, app-supplied embeddings
  services/  catalog.py       dual-write + reconcile
             events.py        queue + batched consumer
             profile.py       time-decayed affinity (no LLM)
             triggers.py      the four spend gates (no LLM)
             recommendations.py  runs the agent, persists, background refresh
             mailer.py        outbox | smtp
  agent/  state.py  nodes.py  graph.py  prompts.py
  routers/  auth pages events recs admin
  scheduler.py       APScheduler: digest, reconcile, housekeeping
tests/    45 tests, no network
seed/products.json   56 courses    scripts/seed.py
```

---

## 1 · Tracking that can't slow the site down

**Client** ([tracker.js](app/static/js/tracker.js), ~400 lines, no dependencies):

- A queue flushed on whichever comes first: 15 events, 5s idle, tab hidden, or
  page unload. Clicking around never issues a request per click.
- `navigator.sendBeacon` on unload, `fetch(keepalive)` otherwise — the final
  batch survives the page going away without blocking navigation.
- Throttled per event class: scroll depth fires once each at 25/50/75/100%;
  search is debounced 600ms so typing "agentic ai" is **one** event, not eleven;
  dwell stops accruing when the tab is backgrounded.
- Each event carries a client-generated `event_id`, so a retried flush can't
  double-count. A `localStorage` copy replays a batch lost to a crash, and only
  the ids the server confirmed are cleared.
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
  metadata (`category`, `level`, `price`, `rating`, `tags`) for filtered
  retrieval.
- **delete** → removed from both in one unit.
- **failure** → the SQL write still commits with `vector_state='pending'` and
  the admin's request succeeds; a repair job fixes it rather than 500-ing.

`reconcile()` runs at startup and every 5 minutes: retries pending rows, deletes
orphan vectors, and re-embeds anything whose stored fingerprint disagrees with
SQL — including when the *embedding provider* changed, which is what makes the
Mesh/local fallback switch safe.

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
  inferred goal, and skill-gap hypotheses (strict JSON).
- **decide_retrieval** is a genuine branch: a user who has done nothing never
  reaches the LLM path at all.
- **multi_query_retrieve** issues 3–4 *distinct* queries (top interest, inferred
  goal, adjacent skill) against Chroma with metadata filters, then fuses the
  result sets with **reciprocal rank fusion**.
- **grade_relevance** scores the candidate pool; a weak pool routes to
  `refine_query`, capped at 2 attempts so the loop provably terminates.
- **rerank_and_select** blends similarity, RRF rank, multi-query agreement and
  category affinity into a *topical* score, then modulates it multiplicatively
  by novelty, level fit, rating and price fit. The distinction matters: hygiene
  factors can only re-order things that are already relevant, never promote
  something irrelevant. A per-category cap spreads the slate.
- **generate_narrative** writes copy that names the user's actual behaviour
  ("you searched for agent memory, tool calling and LangGraph agents, and
  lingered on Planning and Reflection Loops"), plus a per-course reason.
- **validate_grounding** drops any id that wasn't retrieved and flags a
  narrative that quotes a course we don't stock.

The full node path, timings, queries and refine count are stored on the
recommendation and rendered at `/admin/traces` — so the workflow stays
inspectable with or without LangSmith.

**What "grounded" means here:** the model never invents a course. It picks from
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
traces natively; the Mesh client's `chat` and `embed` carry `@traceable` spans
with the API key stripped from the payload. With no key the decorator returns
the function untouched — no spans, no overhead, nothing broken. The local
`/admin/traces` viewer is always there regardless.

**Retrieval polish** — multi-query expansion, RRF fusion, metadata filtering
(published, level, price band), candidate-set-normalised scoring (so ranking is
invariant to the embedding provider's scale), multiplicative reranking, and a
per-category cap.

---

## Verifying the claims

```bash
pytest            # 45 tests, no network required
```

The tests cover the four places a subtle bug would be invisible: the reranker's
weighting, the profile decay maths and signature stability, the trigger gates,
and the grounding validator.

By hand:

- **Non-blocking tracking** — DevTools Network: batched `POST /api/events` every
  few seconds, not per click; `202` in single-digit ms; a beacon on tab close;
  one event for a typed search. Point `MESH_API_BASE_URL` at a dead host and the
  site still browses fine.
- **Dual-write** — add a course in admin, confirm the SQL row and the Chroma
  vector. Edit the title → re-embedded. Edit only the price → **not** re-embedded.
  Delete → gone from both. `/admin/sync` reports the counts.
- **Behaviour drives the output** — run both personas above; the narratives and
  slates diverge, and every recommended id exists in the catalog.
- **Efficiency** — click through 20 courses, then reload the dashboard
  repeatedly and watch `/admin/ai-usage`: the call count doesn't move, because
  the signature didn't.
- **Scheduler** — `/admin/scheduler` → *Run digests*. An HTML digest lands in
  `outbox/` and at `/inbox`, `digest_log` gains one row, and pressing it again
  adds none.

---

## Notes

- **Secrets**: `.env` is gitignored from the first commit; `.env.example`
  documents every key. No credential is needed to read this code.
- SQLite runs in WAL mode so the event writer never blocks page reads. The
  schema is plain SQLAlchemy — `DATABASE_URL` can point at PostgreSQL unchanged.
- APScheduler in-process is right for a single-node hackathon build. For a
  multi-worker deploy the digest job belongs in Celery Beat (or a `--scheduler`
  process running alone); the DB lease in `triggers.py` already handles
  multi-process generation correctly.
- The vector store is behind a small interface, so swapping Chroma for pgvector
  is one file.
