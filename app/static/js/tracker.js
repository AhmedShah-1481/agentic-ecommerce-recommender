/**
 * LearnLoop behavioural tracker.
 *
 * Design constraints from the brief: tracking must be efficient and MUST NOT
 * slow down or break the frontend.
 *
 *   - Batched: events queue in memory and flush on a size/time threshold,
 *     never one request per action.
 *   - Non-blocking: sendBeacon on page hide/unload (survives navigation),
 *     fetch(keepalive) otherwise. Never awaited on a user code path.
 *   - Throttled: scroll sampled at depth milestones, searches debounced,
 *     mousemove never sent, dwell paused while the tab is hidden.
 *   - Idempotent: every event carries a client-generated id, so a retried
 *     flush cannot double-count server-side.
 *   - Crash-safe: an in-flight batch is mirrored to localStorage and replayed
 *     on next load.
 *   - Fail-silent: the whole module is wrapped so a tracking bug can never
 *     break the page.
 */
(function () {
  "use strict";

  var ENDPOINT = "/api/events";
  // Unload path. Separate endpoint because it always answers 2xx: a beacon
  // cannot be retried, so a validation error there is pure log noise.
  var BEACON_ENDPOINT = "/api/events/beacon";
  var MAX_BATCH = 15;          // flush once this many events are queued
  var FLUSH_INTERVAL_MS = 5000; // ...or this often, whichever comes first
  var SEARCH_DEBOUNCE_MS = 600; // "agentic ai" typed = 1 event, not 11
  var MAX_QUEUE = 200;          // hard cap; drop oldest rather than grow
  var BACKUP_KEY = "ll_pending_events";
  var SESSION_KEY = "ll_session_id";

  var queue = [];
  var timer = null;
  var sending = false;
  var scrollMilestones = {};
  var searchTimer = null;
  var pageEnteredAt = Date.now();
  var visibleMs = 0;
  var lastVisibleAt = document.visibilityState === "visible" ? Date.now() : null;
  var dwellSent = false;

  // ---------------------------------------------------------------- utils
  function uuid() {
    try {
      if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
    } catch (e) {}
    return "e-" + Date.now() + "-" + Math.random().toString(16).slice(2, 10);
  }

  function sessionId() {
    try {
      var id = sessionStorage.getItem(SESSION_KEY);
      if (!id) {
        id = uuid();
        sessionStorage.setItem(SESSION_KEY, id);
      }
      return id;
    } catch (e) {
      return "no-storage";
    }
  }

  var SESSION_ID = sessionId();

  function context() {
    var el = document.querySelector("[data-ll-page]");
    return {
      path: location.pathname,
      page: el ? el.getAttribute("data-ll-page") : "",
      product_id: el && el.getAttribute("data-ll-product-id")
        ? parseInt(el.getAttribute("data-ll-product-id"), 10)
        : null
    };
  }

  // -------------------------------------------------------------- queueing
  function track(type, payload) {
    try {
      var ctx = context();
      var ev = {
        event_id: uuid(),
        event_type: type,
        session_id: SESSION_ID,
        path: ctx.path,
        client_ts: new Date().toISOString(),
        product_id: (payload && payload.product_id) || ctx.product_id || null,
        search_query: (payload && payload.search_query) || "",
        dwell_ms: (payload && payload.dwell_ms) || 0,
        scroll_pct: (payload && payload.scroll_pct) || 0,
        meta: (payload && payload.meta) || {}
      };

      queue.push(ev);
      if (queue.length > MAX_QUEUE) queue.splice(0, queue.length - MAX_QUEUE);

      if (queue.length >= MAX_BATCH) flush();
      else scheduleFlush();
    } catch (e) {
      /* never break the page */
    }
  }

  function scheduleFlush() {
    if (timer !== null) return;
    timer = setTimeout(function () {
      timer = null;
      flush();
    }, FLUSH_INTERVAL_MS);
  }

  function backup(batch) {
    // Merge rather than overwrite: two batches can be un-confirmed at once (a
    // fetch in flight when the page hides and beacons the rest). A single-slot
    // write would silently discard the other one's crash backup. Deduped by
    // event_id so a replay after a partial success can't double up.
    try {
      var seen = {};
      var merged = [];
      var existing = [];
      try {
        existing = JSON.parse(localStorage.getItem(BACKUP_KEY) || "[]") || [];
      } catch (e) {}

      existing.concat(batch).forEach(function (ev) {
        if (ev && ev.event_id && !seen[ev.event_id]) {
          seen[ev.event_id] = true;
          merged.push(ev);
        }
      });

      localStorage.setItem(BACKUP_KEY, JSON.stringify(merged.slice(-MAX_QUEUE)));
    } catch (e) {}
  }

  function clearBackup() {
    try {
      localStorage.removeItem(BACKUP_KEY);
    } catch (e) {}
  }

  /**
   * Drop only the events the server has confirmed, leaving any other
   * un-confirmed batch's mirror intact. Removing the whole key here would
   * discard a concurrent batch's crash backup.
   */
  function confirmBackup(batch) {
    try {
      var done = {};
      batch.forEach(function (ev) {
        if (ev && ev.event_id) done[ev.event_id] = true;
      });

      var remaining = (JSON.parse(localStorage.getItem(BACKUP_KEY) || "[]") || []).filter(
        function (ev) {
          return ev && ev.event_id && !done[ev.event_id];
        }
      );

      if (remaining.length) localStorage.setItem(BACKUP_KEY, JSON.stringify(remaining));
      else clearBackup();
    } catch (e) {
      clearBackup();
    }
  }

  /**
   * Send queued events.
   * @param {boolean} useBeacon - true on page hide/unload, where fetch may be
   *   cancelled by the navigation.
   */
  function flush(useBeacon) {
    if (queue.length === 0) return;
    // A fetch already in flight blocks another fetch (we'd double-send), but
    // must NOT block a beacon: the page is going away and this is the last
    // chance to ship what is queued. The in-flight batch is a different set of
    // events, and event_id makes any overlap harmless server-side.
    if (sending && !useBeacon) return;

    if (timer !== null) {
      clearTimeout(timer);
      timer = null;
    }

    var batch = queue.slice();
    queue = [];
    var body = JSON.stringify({ events: batch });

    // Page is going away: beacon is the only reliable transport.
    if (useBeacon && navigator.sendBeacon) {
      try {
        var ok = navigator.sendBeacon(
          BEACON_ENDPOINT,
          new Blob([body], { type: "application/json" })
        );
        // sendBeacon returning false means the browser refused to queue it
        // (usually over the payload cap). Mirror it so next load replays it -
        // there is no callback to learn about a later failure.
        if (!ok) backup(batch);
        return;
      } catch (e) {
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
        body: body,
        keepalive: true,
        credentials: "same-origin"
      })
        .then(function (res) {
          sending = false;
          if (res && (res.ok || res.status === 202)) {
            confirmBackup(batch);
          } else {
            requeue(batch);
          }
        })
        .catch(function () {
          sending = false;
          requeue(batch); // offline / server down - try again next flush
        });
    } catch (e) {
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
      var raw = localStorage.getItem(BACKUP_KEY);
      if (!raw) return;
      clearBackup();
      var batch = JSON.parse(raw);
      if (Array.isArray(batch) && batch.length) {
        queue = batch.concat(queue).slice(-MAX_QUEUE);
        scheduleFlush();
      }
    } catch (e) {
      clearBackup();
    }
  }

  // ------------------------------------------------------------- listeners
  function trackScroll() {
    // Only 4 events per page, no matter how much the user scrolls.
    var doc = document.documentElement;
    var scrollable = doc.scrollHeight - window.innerHeight;
    if (scrollable <= 0) return;
    var pct = Math.round((window.scrollY / scrollable) * 100);
    var milestones = [25, 50, 75, 100];
    for (var i = 0; i < milestones.length; i++) {
      var m = milestones[i];
      if (pct >= m && !scrollMilestones[m]) {
        scrollMilestones[m] = true;
        track("scroll_depth", { scroll_pct: m });
      }
    }
  }

  function currentDwellMs() {
    var total = visibleMs;
    if (lastVisibleAt !== null) total += Date.now() - lastVisibleAt;
    return total;
  }

  function sendDwell(useBeacon) {
    if (dwellSent) return;
    var ms = currentDwellMs();
    if (ms < 1500) return; // ignore bounces
    dwellSent = true;
    track("dwell", { dwell_ms: ms, scroll_pct: maxScrollPct() });
    flush(useBeacon);
  }

  function maxScrollPct() {
    var best = 0;
    for (var k in scrollMilestones) if (scrollMilestones[k]) best = Math.max(best, parseInt(k, 10));
    return best;
  }

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
      dwellSent = false;
    }
  }

  function bindSearch() {
    var inputs = document.querySelectorAll("[data-ll-search]");
    Array.prototype.forEach.call(inputs, function (input) {
      input.addEventListener(
        "input",
        function () {
          if (searchTimer) clearTimeout(searchTimer);
          var value = input.value;
          searchTimer = setTimeout(function () {
            var q = (value || "").trim();
            if (q.length >= 2) track("search", { search_query: q });
          }, SEARCH_DEBOUNCE_MS);
        },
        { passive: true }
      );
    });
  }

  function bindClicks() {
    // Delegated: one listener regardless of how many cards are on the page.
    document.addEventListener(
      "click",
      function (evt) {
        try {
          var el = evt.target.closest ? evt.target.closest("[data-ll-track-click]") : null;
          if (!el) return;
          var type = el.getAttribute("data-ll-track-click") || "click";
          var pid = el.getAttribute("data-ll-product-id");
          track(type, {
            product_id: pid ? parseInt(pid, 10) : null,
            meta: { label: (el.getAttribute("data-ll-label") || el.textContent || "").trim().slice(0, 80) }
          });
        } catch (e) {}
      },
      { passive: true, capture: true }
    );
  }

  // ------------------------------------------------------------------ init
  function init() {
    try {
      replayBackup();

      var ctx = context();
      track(ctx.product_id ? "product_view" : "page_view", { meta: { page: ctx.page } });

      var scrollTimer = null;
      window.addEventListener(
        "scroll",
        function () {
          // rAF-throttled: scroll fires continuously, we sample it.
          if (scrollTimer !== null) return;
          scrollTimer = requestAnimationFrame(function () {
            scrollTimer = null;
            trackScroll();
          });
        },
        { passive: true }
      );

      document.addEventListener("visibilitychange", onVisibilityChange);
      window.addEventListener("pagehide", function () {
        sendDwell(true);
        flush(true);
      });

      bindSearch();
      bindClicks();
      scheduleFlush();
    } catch (e) {
      /* tracking is never allowed to break the site */
    }
  }

  // Public surface for inline page hooks (e.g. add-to-cart).
  window.LLTrack = {
    track: track,
    flush: function () {
      flush(false);
    }
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
