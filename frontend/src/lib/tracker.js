/**
 * Behavioural tracker - the React port of the storefront's `tracker.js`.
 *
 * The transport is carried over unchanged, because the guarantees the backend
 * depends on live there:
 *
 *   - **Batched.** Events queue in memory and flush on a size or time
 *     threshold, never one request per action.
 *   - **Non-blocking.** `sendBeacon` on hide/unload (survives navigation),
 *     `fetch(keepalive)` otherwise. Never awaited on a user code path.
 *   - **Throttled.** Scroll sampled at depth milestones, search debounced,
 *     dwell paused while the tab is hidden.
 *   - **Idempotent.** Every event carries a client-generated `event_id`, so a
 *     retried flush cannot double-count server-side.
 *   - **Crash-safe.** An un-confirmed batch is mirrored to localStorage and
 *     replayed on the next load.
 *   - **Fail-silent.** Every entry point is wrapped: a tracking bug must not
 *     be able to break the store.
 *
 * Three things had to change for a single-page app:
 *
 *   1. Context is pushed, not scraped. The MPA read `[data-ll-page]` out of the
 *      DOM on load; here each route calls `setContext()` and the queue reads
 *      whatever is current. Nothing needs to query the DOM at all.
 *
 *   2. A route change is a page boundary. In the MPA, unload ended the page and
 *      flushed dwell. A client-side navigation fires no unload, so
 *      `startPage()` closes the previous page out - shipping its dwell and
 *      resetting the scroll milestones - before opening the next. Without that,
 *      one tab's dwell would grow all session and only four scroll events would
 *      ever be recorded.
 *
 *   3. Cross-origin transport. `credentials: "include"` because the API is on
 *      another port, and the beacon Blob is sent as `text/plain` - a
 *      CORS-safelisted type, so it needs no preflight. A preflight on unload is
 *      exactly the request a browser is most likely to abandon, and
 *      `/api/events/beacon` already parses its body without trusting the
 *      content-type for precisely this reason.
 */
import { API_BASE } from "./api";

const ENDPOINT = `${API_BASE}/api/events`;
// Unload path. Separate endpoint because it always answers 2xx: a beacon cannot
// be retried, so a validation error there is pure log noise.
const BEACON_ENDPOINT = `${API_BASE}/api/events/beacon`;

const MAX_BATCH = 15; // flush once this many events are queued
const FLUSH_INTERVAL_MS = 5000; // ...or this often, whichever comes first
const SEARCH_DEBOUNCE_MS = 600; // "merino sweater" typed = 1 event, not 14
const MAX_QUEUE = 200; // hard cap; drop oldest rather than grow
const MIN_DWELL_MS = 1500; // below this it is a bounce, not engagement
const FILTER_DEDUP_MS = 30000; // a refresh or a Next click is not new interest

const BACKUP_KEY = "ll_pending_events";
const SESSION_KEY = "ll_session_id";
const FILTER_KEY = "ll_last_filter";

let queue = [];
let timer = null;
let sending = false;

let context = { path: "/", page: "", productId: null };
let scrollMilestones = {};
let searchTimer = null;
let visibleMs = 0;
let lastVisibleAt = null;
let dwellSent = false;
let started = false;

// ------------------------------------------------------------------- utils
function uuid() {
  try {
    if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
  } catch {
    /* fall through */
  }
  return `e-${Date.now()}-${Math.random().toString(16).slice(2, 10)}`;
}

function sessionId() {
  try {
    let id = sessionStorage.getItem(SESSION_KEY);
    if (!id) {
      id = uuid();
      sessionStorage.setItem(SESSION_KEY, id);
    }
    return id;
  } catch {
    return "no-storage";
  }
}

const SESSION_ID = sessionId();

// ---------------------------------------------------------------- queueing
export function track(type, payload) {
  try {
    const event = {
      event_id: uuid(),
      event_type: type,
      session_id: SESSION_ID,
      path: context.path,
      client_ts: new Date().toISOString(),
      product_id: (payload && payload.product_id) || context.productId || null,
      search_query: (payload && payload.search_query) || "",
      dwell_ms: (payload && payload.dwell_ms) || 0,
      scroll_pct: (payload && payload.scroll_pct) || 0,
      meta: (payload && payload.meta) || {},
    };

    queue.push(event);
    if (queue.length > MAX_QUEUE) queue.splice(0, queue.length - MAX_QUEUE);

    if (queue.length >= MAX_BATCH) flush();
    else scheduleFlush();
  } catch {
    /* never break the page */
  }
}

function scheduleFlush() {
  if (timer !== null) return;
  timer = setTimeout(() => {
    timer = null;
    flush();
  }, FLUSH_INTERVAL_MS);
}

/**
 * Mirror an un-confirmed batch.
 *
 * Merges rather than overwrites: two batches can be un-confirmed at once (a
 * fetch in flight when the page hides and beacons the rest). A single-slot
 * write would silently discard the other one's crash backup. Deduped by
 * event_id so a replay after a partial success cannot double up.
 */
function backup(batch) {
  try {
    const seen = {};
    const merged = [];
    let existing = [];
    try {
      existing = JSON.parse(localStorage.getItem(BACKUP_KEY) || "[]") || [];
    } catch {
      existing = [];
    }

    existing.concat(batch).forEach((event) => {
      if (event && event.event_id && !seen[event.event_id]) {
        seen[event.event_id] = true;
        merged.push(event);
      }
    });

    localStorage.setItem(BACKUP_KEY, JSON.stringify(merged.slice(-MAX_QUEUE)));
  } catch {
    /* storage blocked or full - the queue is still in memory */
  }
}

function clearBackup() {
  try {
    localStorage.removeItem(BACKUP_KEY);
  } catch {
    /* nothing to do */
  }
}

/**
 * Drop only the events the server confirmed, leaving any other un-confirmed
 * batch's mirror intact. Removing the whole key here would discard a
 * concurrent batch's crash backup.
 */
function confirmBackup(batch) {
  try {
    const done = {};
    batch.forEach((event) => {
      if (event && event.event_id) done[event.event_id] = true;
    });

    const remaining = (JSON.parse(localStorage.getItem(BACKUP_KEY) || "[]") || []).filter(
      (event) => event && event.event_id && !done[event.event_id]
    );

    if (remaining.length) localStorage.setItem(BACKUP_KEY, JSON.stringify(remaining));
    else clearBackup();
  } catch {
    clearBackup();
  }
}

/**
 * Send queued events.
 * @param {boolean} useBeacon true on hide/unload, where a fetch may be
 *   cancelled by the navigation.
 */
export function flush(useBeacon = false) {
  if (queue.length === 0) return;
  // A fetch already in flight blocks another fetch (we would double-send) but
  // must NOT block a beacon: the page is going away and this is the last chance
  // to ship what is queued. The in-flight batch is a different set of events,
  // and event_id makes any overlap harmless server-side.
  if (sending && !useBeacon) return;

  if (timer !== null) {
    clearTimeout(timer);
    timer = null;
  }

  const batch = queue.slice();
  queue = [];
  const body = JSON.stringify({ events: batch });

  if (useBeacon && navigator.sendBeacon) {
    try {
      // text/plain, not application/json: a JSON content-type makes this a
      // non-simple cross-origin request, and the preflight it needs is the one
      // a browser is most likely to drop during unload. The beacon endpoint
      // parses the body regardless of what the header claims.
      const ok = navigator.sendBeacon(
        BEACON_ENDPOINT,
        new Blob([body], { type: "text/plain;charset=UTF-8" })
      );
      // `false` means the browser refused to queue it (usually over the payload
      // cap). Mirror it so the next load replays it - there is no callback to
      // learn about a later failure.
      if (!ok) backup(batch);
      return;
    } catch {
      backup(batch);
      return;
    }
  }

  sending = true;
  backup(batch); // mirrored until the POST is confirmed

  try {
    fetch(ENDPOINT, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body,
      keepalive: true,
      // Cross-origin now, so "same-origin" would strip the session cookie and
      // every event would land as anonymous - the pipeline would look healthy
      // while attributing nothing to the signed-in user.
      credentials: "include",
    })
      .then((response) => {
        sending = false;
        if (response && (response.ok || response.status === 202)) confirmBackup(batch);
        else requeue(batch);
      })
      .catch(() => {
        sending = false;
        requeue(batch); // offline / API down - try again next flush
      });
  } catch {
    sending = false;
    requeue(batch);
  }
}

function requeue(batch) {
  // event_id makes a duplicate delivery harmless, so retrying is safe.
  queue = batch.concat(queue).slice(-MAX_QUEUE);
  scheduleFlush();
}

function replayBackup() {
  try {
    const raw = localStorage.getItem(BACKUP_KEY);
    if (!raw) return;
    clearBackup();
    const batch = JSON.parse(raw);
    if (Array.isArray(batch) && batch.length) {
      queue = batch.concat(queue).slice(-MAX_QUEUE);
      scheduleFlush();
    }
  } catch {
    clearBackup();
  }
}

// ------------------------------------------------------------------- dwell
function currentDwellMs() {
  let total = visibleMs;
  if (lastVisibleAt !== null) total += Date.now() - lastVisibleAt;
  return total;
}

function maxScrollPct() {
  return Object.keys(scrollMilestones).reduce(
    (best, key) => (scrollMilestones[key] ? Math.max(best, parseInt(key, 10)) : best),
    0
  );
}

function sendDwell(useBeacon) {
  if (dwellSent) return;
  const ms = currentDwellMs();
  if (ms < MIN_DWELL_MS) return; // ignore bounces
  dwellSent = true;
  track("dwell", { dwell_ms: ms, scroll_pct: maxScrollPct() });
  flush(useBeacon);
}

// --------------------------------------------------------------- page life
/**
 * Open a new page.
 *
 * Called on every route change. Closes the previous page out first: its dwell
 * is the whole reason `product_view` is worth anything, and in an SPA no unload
 * event will ever ship it.
 */
export function startPage({ path, page = "", productId = null }) {
  try {
    if (started) {
      sendDwell(false);
      flush(false);
    }

    context = { path, page, productId: productId ?? null };
    scrollMilestones = {};
    visibleMs = 0;
    lastVisibleAt = document.visibilityState === "visible" ? Date.now() : null;
    dwellSent = false;
    started = true;

    // A page with a product is a product view; anything else is a page view.
    // Same split the MPA made, and `product_view` is what the profile scores.
    track(context.productId ? "product_view" : "page_view", { meta: { page } });
  } catch {
    /* never break the page */
  }
}

// ------------------------------------------------------------- interactions
/** A click on a product card, an add to cart, a CTA. */
export function trackClick(type, { productId = null, label = "" } = {}) {
  track(type, {
    product_id: productId,
    meta: label ? { label: String(label).slice(0, 80) } : {},
  });
}

/**
 * A typed search. Debounced, so a phrase is one event rather than one per
 * keystroke, and ignored under two characters.
 */
export function trackSearch(value) {
  if (searchTimer) clearTimeout(searchTimer);
  const query = (value || "").trim();
  searchTimer = setTimeout(() => {
    if (query.length >= 2) track("search", { search_query: query });
  }, SEARCH_DEBOUNCE_MS);
}

/**
 * Emit the active catalog facet as a `search` event.
 *
 * Reuses `search` rather than a new event type on purpose: it is already in
 * MEANINGFUL_TYPES (so it moves the momentum gate), already weighted 3.0, and
 * already feeds `recent_searches`, which the agent turns into retrieval
 * queries. A new type would need changes in ingest, scoring and the agent to do
 * the same work. `meta.source` is what lets the signal panel label it
 * "Filtered" rather than "Searched".
 *
 * The typed query is deliberately excluded from `facet` - the search box fires
 * its own event, and including it here would count one search twice.
 */
export function trackFilter(facetValues) {
  try {
    const facet = (facetValues || []).filter(Boolean).join(" ").trim();
    if (!facet) return;

    // Reloading or paging through the same filtered view is not new interest,
    // and counting it would inflate events_since_last_rec into a regeneration
    // the user never earned. Re-picking the category later still counts.
    try {
      const previous = JSON.parse(sessionStorage.getItem(FILTER_KEY) || "null");
      if (previous && previous.facet === facet && Date.now() - previous.at < FILTER_DEDUP_MS) return;
      sessionStorage.setItem(FILTER_KEY, JSON.stringify({ facet, at: Date.now() }));
    } catch {
      /* storage blocked - fire anyway rather than lose the signal */
    }

    track("search", { search_query: facet, meta: { source: "filter" } });
  } catch {
    /* never break the page */
  }
}

// -------------------------------------------------------------------- init
function onVisibilityChange() {
  if (document.visibilityState === "hidden") {
    if (lastVisibleAt !== null) {
      visibleMs += Date.now() - lastVisibleAt;
      lastVisibleAt = null;
    }
    // Backgrounded tabs stop accruing dwell, and we ship what we have.
    sendDwell(true);
    flush(true);
  } else {
    lastVisibleAt = Date.now();
    // A returning tab is a fresh look at the same page, so it can earn another
    // dwell event - the accumulator keeps running from where it stopped.
    dwellSent = false;
  }
}

function trackScroll() {
  // At most four events per page, no matter how far the user scrolls.
  const scrollable = document.documentElement.scrollHeight - window.innerHeight;
  if (scrollable <= 0) return;
  const pct = Math.round((window.scrollY / scrollable) * 100);
  [25, 50, 75, 100].forEach((milestone) => {
    if (pct >= milestone && !scrollMilestones[milestone]) {
      scrollMilestones[milestone] = true;
      track("scroll_depth", { scroll_pct: milestone });
    }
  });
}

let initialised = false;

/** Bind the window-level listeners. Idempotent - StrictMode mounts twice. */
export function initTracker() {
  if (initialised) return;
  initialised = true;

  try {
    replayBackup();

    let frame = null;
    window.addEventListener(
      "scroll",
      () => {
        // rAF-throttled: scroll fires continuously, we sample it.
        if (frame !== null) return;
        frame = requestAnimationFrame(() => {
          frame = null;
          trackScroll();
        });
      },
      { passive: true }
    );

    document.addEventListener("visibilitychange", onVisibilityChange);
    window.addEventListener("pagehide", () => {
      sendDwell(true);
      flush(true);
    });

    scheduleFlush();
  } catch {
    /* tracking is never allowed to break the site */
  }
}
