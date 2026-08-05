# Project state — LearnLoop

Working notes for picking this up cold. Read [README.md](README.md) for what the
project *is*; this file is about where it stands, what's still open, and why
things were built the way they were.

**Status: complete. All requirements plus all four bonuses. 67 tests passing.
Deadline: 11 Aug.** The "Your Signal" panel, the Add-to-Cart button and facet
tracking are implemented and tested but **not yet committed** — see §2(b).

---

## 1 · Done and verified

Every item below was checked by running it, not by reading the code.

### The platform
- FastAPI + Jinja2 + SQLAlchemy 2.0 + SQLite (WAL mode).
- bcrypt password hashing (bcrypt 5.0.0 directly, no passlib), signed httponly
  session cookie via `itsdangerous`.
- Two roles: `user` and `admin`, enforced by `current_user` / `require_admin`
  dependencies in [app/security.py](app/security.py).
- 8 related tables in [app/models.py](app/models.py): users, products, events,
  user_profile_state, recommendations, recommendation_items, llm_calls,
  digest_log.
- Pages: `/` catalog with search + category/level filters + pagination,
  `/product/{id}`, `/login`, `/register`, `/dashboard`, `/inbox`.
- Admin: product CRUD, `/admin/sync`, `/admin/ai-usage`, `/admin/traces`,
  `/admin/events`, `/admin/scheduler`.
- **Verified:** all routes return expected status codes under `TestClient`
  (`/healthz` 200, `/` 200, `/login` 200, `/product/1` 200, `/dashboard` 303
  when anonymous, `POST /api/events` 202).

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
  disagrees with what's stored.
- **Verified:** `sql=56 vector=56 in_sync=True`, provider honestly reported as
  `local:hash512`, reconcile repairs 0 on a clean store.

### Behavioural tracking
- [app/static/js/tracker.js](app/static/js/tracker.js), ~400 lines, no deps.
  Queue flushed on 15 events / 5s idle / tab hidden / unload.
  `sendBeacon` on unload, `fetch(keepalive)` otherwise.
- Throttling: scroll depth once each at 25/50/75/100%, search debounced 600ms,
  dwell stops accruing on a backgrounded tab.
- Client-generated `event_id` for idempotency; `localStorage` backup merges and
  dedupes, and only server-confirmed ids are cleared.
- Ingest ([app/routers/events.py](app/routers/events.py)) **opens no database
  connection at all** — the user id comes from the signed cookie
  (`optional_user_id`), not a lookup, because this is the busiest endpoint.
  Returns 202; a background consumer writes batches with one multi-row INSERT.
- Bounded queue sheds *oldest* events under flood; shutdown flushes what's
  queued.
- **Verified:** `POST /api/events` 202, events land in the DB via the consumer,
  per-type counts visible at `/admin/events`.
- Catalog **facet selections are tracked** (`category` / `level`). This was a
  real blind spot found by browsing the running app: choosing a category
  navigates, and `tracker.js` classifies that as a bare `page_view` — which is
  excluded from `MEANINGFUL_TYPES`, carries no `product_id`, and the facet name
  only ever exists in the query string, which the tracker never reads. Filtering
  through seven categories therefore produced **zero** signal.
  Fixed in [app/static/js/ui.js](app/static/js/ui.js) `trackFilter()`: the facet
  is emitted as a `search` event with `meta.source = "filter"`, through the
  public `window.LLTrack.track` hook. Reusing `search` rather than adding an
  event type means it inherits weight 3.0, `MEANINGFUL_TYPES` membership, and
  `recent_searches` — which the agent turns into retrieval queries
  ([nodes.py:79](app/agent/nodes.py#L79)) — with no change to ingest, scoring or
  the agent. `meta.source` is what lets the "Your Signal" panel label it
  *Filtered* rather than claiming the user typed a query. A `sessionStorage`
  guard suppresses an identical facet within 30s so a reload or a pagination
  click can't inflate `events_since_last_rec`.
  **Known bound:** `has_signal()` requires a non-empty `category_affinity`, and
  only events carrying a `product_id` populate that. So filtering alone still
  won't cold-start a *first* recommendation — one course must be opened.
  Filtering does move momentum, `high_signal`, tag affinity and retrieval.
  Pinned by `test_a_facet_survives_the_real_ingest_path` (through the real
  `_write_batch`) and `test_filtering_alone_still_cannot_cold_start`.

### The agent
- LangGraph `StateGraph` in [app/agent/graph.py](app/agent/graph.py):
  `analyze_behavior → decide_retrieval →(skip) cold_start | multi_query_retrieve
  → grade_relevance →(weak, max 2) refine_query | rerank_and_select →
  generate_narrative → validate_grounding`.
- Multi-query retrieval (3–4 distinct queries) + **reciprocal rank fusion**
  (`RRF_K = 60`) + metadata filters (published, level, price band).
- Reranking is **multiplicative**: topical fit (similarity, RRF, multi-query
  agreement, category affinity) is the only thing that makes an item eligible;
  novelty / level fit / rating / price fit modulate it. Similarity is normalised
  against the candidate set, so ranking is invariant to embedding-provider scale.
- `validate_grounding` drops any product id that wasn't retrieved and flags a
  narrative quoting a course we don't stock. `_persist` re-checks every id
  against the products table before writing.
- **Verified on two personas** (`agentic` and `data`), producing distinct,
  on-topic, LLM-authored slates with `model: tencent/hy3`, `fallback=False`:
  - agentic → Planning/Reflection Loops, Multi-Agent Collaboration,
    Fine-Tuning LoRA, Shipping Agents to Production
  - data → Model Monitoring, dbt/Snowflake, Airflow, Feature Stores
  - reranker moved dbt/Snowflake 9th → 2nd and dropped an off-topic Figma
    course out of the top 12 for the data persona.

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
  `queued: false` with the dashboard button reading *"Up to date (cooldown)"*.
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
- `mesh_client.chat` and `.embed` carry spans; the input formatter drops `self`
  so the API key can't reach the trace payload.
- The local `/admin/traces` viewer works regardless, so the workflow is always
  inspectable.
- **Not yet exercised against a live LangSmith account** — see §2.

### Bonus 4 — retrieval polish
Multi-query expansion, RRF fusion, metadata filtering, candidate-set-normalised
scoring, multiplicative reranking, per-category cap (`MAX_PER_CATEGORY = 2`),
below-band filler capped at `MIN_SLATE = 2` so a weak pool doesn't pad the slate
with irrelevance.

### Tests — 67 passing, no network required
```
tests/test_rerank.py      7   reranker weighting properties
tests/test_profile.py    14   decay maths + signature stability + facet intent
tests/test_triggers.py   17   the four gates, lease contention, force semantics
tests/test_grounding.py  10   hallucinated ids and titles
tests/test_signal.py     19   feed wording, cross-user isolation, limit clamp
```

### Git
One commit, `ac1f309`, 61 files. `.env`, `venv/`, `data/`, `outbox/`, `*.db`,
`chroma/` all excluded — the staged list was checked explicitly, not just
trusted to `.gitignore`.

---

## 2 · Pending / open

### (a) Mesh embeddings return 402 — the one real limitation

The Mesh account has no balance. The embeddings endpoint returns:

```
402 {'error': {'code': 'spend_limit_exceeded',
               'message': 'Insufficient balance. Top up your account to use paid models.'}}
```

**Current behaviour, and why it's safe:**
- `PERMANENT_EMBED_FAILURES = {401, 402, 403, 404}` in
  [app/ai/mesh_client.py](app/ai/mesh_client.py) — on any of these the client
  logs once, sets `_embeddings_supported = False` for the process, and stops
  asking. Retrying can't help and every attempt costs latency on a user-facing
  path.
- [app/ai/embeddings.py](app/ai/embeddings.py) falls back to a **local
  deterministic hashed-ngram embedder** (`local:hash512`). Retrieval keeps
  working; it's lexical-ish rather than truly semantic.
- **Chat is unaffected.** `tencent/hy3` runs on the live Mesh API, so the agent
  is genuinely LLM-driven — the narratives and per-course reasons are real model
  output naming the user's actual searches.

**To switch to real semantic retrieval: top up the account. No code change.**
`reconcile()` compares the stored `embed_provider` metadata against the observed
provider and re-embeds the catalog when it changes. On the next startup (or the
5-minute job) all 56 products get real vectors.

Note `resolve_provider()` in embeddings.py: it does one probe embed before
comparing, because `provider_id()` alone was a *guess*. Without the probe, a
cold start on a Mesh-unavailable deployment marked all 56 products
provider-stale and re-embedded the whole catalog every time. Don't remove it.

### (b) Everything else not yet closed

- **The repository is private.** `git push` works (authenticated), but the repo
  page, the raw NOTES.md blob and the GitHub API all return **404 anonymously**,
  so a reviewer cannot open it. Submission needs it public:
  `Settings → Danger Zone → Change repository visibility → Public`. `gh` is not
  installed locally, so this has to be done in the browser.
- **Two features are on disk but uncommitted:** the "Your Signal" panel +
  Add-to-Cart labelling/badge/toast, and facet tracking. 67 tests pass and both
  were checked against a running app. `git status` shows 11 modified and 4 new
  files (`app/services/signal.py`, `app/static/js/ui.js`,
  `app/templates/_signal.html`, `tests/test_signal.py`).
- **LangSmith is wired but never run against a live account.** The code path is
  exercised in the off state (which is the default and degrades silently). If a
  key gets added, check that spans actually arrive and that nothing in the
  payload carries the Mesh key.
- **SMTP backend is untested against a real server.** `outbox` is the default
  and is fully verified; `_send_smtp` is straightforward aiosmtplib but has
  never had a live SMTP host pointed at it.
- **No GitHub Actions / CI.** `pytest` runs clean locally; nothing runs it
  automatically.
- **Digest email rendering only checked in a browser-agnostic way** — the
  template is table-based with inline styles (correct for email clients), but it
  hasn't been through Litmus or a real Gmail/Outlook render.

Nothing here blocks the submission.

---

## 3 · Running it from a clean terminal

Windows, Git Bash or PowerShell. Python 3.13.5 is what's installed here;
3.11+ works.

```bash
cd d:/Hackathon

# 1. Virtual environment (already exists as venv/ — recreate only if missing)
python -m venv venv
venv/Scripts/activate            # Git Bash
# venv\Scripts\activate          # PowerShell / cmd
pip install -r requirements.txt

# 2. Config — .env already exists locally with the Mesh key.
#    On a fresh clone:
cp .env.example .env             # copy on Windows cmd
#    then put MESH_API_KEY in .env.  NEVER commit .env.

# 3. Seed 56 courses + demo accounts (dual-writes to SQL and Chroma)
python -m scripts.seed

# 4. Run
uvicorn app.main:app --reload
```

Then http://127.0.0.1:8000.

| Account | Password | For |
|---|---|---|
| `admin@learnloop.dev` | `admin123` | product CRUD + all admin views |
| `learner@learnloop.dev` | `learner123` | the recommendation dashboard |

**Without installing anything through the venv, nothing works** — every install
in this project went through `venv/Scripts/python -m pip`, never a global pip.

### Demoing the recommender without clicking around

```bash
python -m scripts.seed --simulate-user agentic --recommend
python -m scripts.seed --simulate-user data --recommend
```

Replays a realistic browsing session (searches, views, dwell times), then runs
the agent. Sign in as the learner and open **For you**. The two personas
produce visibly different slates — that's the demo that proves behaviour drives
the output.

### Tests

```bash
venv/Scripts/python -m pytest tests/ -q      # 45 passed, ~4s, no network
```

### Demoing the scheduler without waiting for 16:00 UTC

`/admin/scheduler` → **Run digests**. Same function the cron trigger calls.
Press it twice: the second press is a no-op, which is the `(user_id,
digest_date)` uniqueness doing its job. The digest appears in `outbox/` and at
`/inbox`.

---

## 4 · Known issues / watch before 11 Aug

- **The 402 is the only thing a judge might read as incomplete.** The README
  explains it honestly and the fallback is deliberate engineering rather than a
  workaround, but topping up the Mesh account before submitting would remove the
  question entirely. Worth doing if it's cheap.
- **`venv/` is 400MB+ and gitignored.** Don't commit it. If the repo ever looks
  suspiciously large, that's the first thing to check.
- **`.env` contains a real Mesh key.** It's gitignored from the first commit and
  the staged file list was verified. Re-verify with
  `git diff --cached --name-only` before any future push.
- **`data/app.db` and `data/chroma/` are local state, also gitignored.** A fresh
  clone must run `python -m scripts.seed` or the catalog is empty and the
  recommender has nothing to retrieve.
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

**Mesh embeddings primary, local deterministic fallback.** The brief mandates
that all AI traffic goes through Mesh, so Mesh is tried first and every call
goes through one module. But an embeddings entitlement is separate from chat, and
a submission that dies on a 402 demonstrates nothing. The fallback keeps
retrieval working and — because `reconcile()` detects the provider change —
upgrading to real vectors is a top-up, not a rewrite.

**Chroma with app-supplied embeddings**, not Chroma's built-in embedding
function. If Chroma embedded internally it would pull its own model and AI
traffic would leave the Mesh path, breaking the mandatory constraint.

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
terms (novelty + level fit + rating + price fit) summed to more than category
affinity, so a tidy-metadata course could outrank a relevant one — an MLOps
learner was being shown *Design Systems in Figma*. Topical fit is now the only
thing that makes an item eligible; hygiene factors can only re-order what's
already relevant. `tests/test_rerank.py` pins this.

**Similarity normalised against the candidate set.** Different embedding
providers return different score scales. Normalising per-query means the ranking
is identical whether the vectors came from Mesh or the local fallback.

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
courses" is a hope. A node that drops every id absent from the retrieved set,
plus an id re-check against the products table before persistence, is a
guarantee.

**Outbox filenames are `recipient__timestamp__subject`.** The double underscore
matters: the outbox is a shared directory, and `list_outbox`/`read_outbox` filter
by recipient so one user can't read another's digest at `/inbox/{filename}`.
Single dashes appear inside all three fields, so the address wasn't recoverable
before this change.

**bcrypt directly, no passlib.** passlib's bcrypt backend is broken against
bcrypt 5.x. Calling bcrypt directly is three lines and has no version coupling.

**`tencent/hy3` needs headroom.** It's a reasoning model: it spends hidden
`reasoning_content` tokens before emitting any answer, so a small `max_tokens`
gets entirely consumed by reasoning and returns empty content. Hence
`MIN_REASONING_HEADROOM = 700` and a 120s timeout — both measured against the
live API, not guessed.
