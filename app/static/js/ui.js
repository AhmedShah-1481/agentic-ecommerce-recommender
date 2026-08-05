/**
 * Presentation-only UI helpers: cart badge, toast, and the "Your Signal" panel -
 * plus one behavioural event the tracker cannot capture on its own.
 *
 * The one exception to "presentation only" is trackFilter(): selecting a category
 * navigates, and tracker.js records that as a bare page_view because the catalog
 * carries no product_id and the facet lives in the query string it never reads.
 * So the facet is emitted here, through the PUBLIC window.LLTrack.track hook -
 * this file still never touches the tracker's queue, timers or transport.
 *
 * Everything else is drawing only. In particular the cart badge cannot
 * double-count add_to_cart: tracker.js already captures that click through its
 * delegated [data-ll-track-click] listener, and this file only paints the result.
 *
 * Same fail-silent discipline as the tracker: everything is wrapped, so a
 * missing element or a blocked storage API degrades to "no badge" rather than
 * a broken page.
 */
(function () {
  "use strict";

  var CART_KEY = "ll_cart_count";
  var FILTER_KEY = "ll_last_filter";
  var FILTER_DEDUP_MS = 30000;  // a refresh or a Next click is not new interest
  var SIGNAL_ENDPOINT = "/api/me/signal";
  var SIGNAL_POLL_MS = 4000;
  var TOAST_MS = 2600;

  // ------------------------------------------------------------------ cart
  function readCount() {
    try {
      return parseInt(sessionStorage.getItem(CART_KEY) || "0", 10) || 0;
    } catch (e) {
      return 0;
    }
  }

  function writeCount(n) {
    try {
      sessionStorage.setItem(CART_KEY, String(n));
    } catch (e) {}
  }

  function paintCart(n) {
    var chip = document.getElementById("cart-chip");
    var out = document.getElementById("cart-count");
    if (!chip || !out) return;
    out.textContent = String(n);
    if (n > 0) chip.removeAttribute("hidden");
    else chip.setAttribute("hidden", "");
    // Retrigger the bump animation: removing and re-adding in one frame is a
    // no-op, so force a reflow between.
    chip.classList.remove("bump");
    void chip.offsetWidth;
    chip.classList.add("bump");
  }

  // ----------------------------------------------------------------- toast
  function toast(message) {
    try {
      var wrap = document.getElementById("toast-wrap");
      if (!wrap) return;
      var el = document.createElement("div");
      el.className = "toast";
      el.textContent = message;
      wrap.appendChild(el);
      // Next frame, so the transition has a start state to animate from.
      requestAnimationFrame(function () {
        el.classList.add("in");
      });
      setTimeout(function () {
        el.classList.remove("in");
        setTimeout(function () {
          if (el.parentNode) el.parentNode.removeChild(el);
        }, 260);
      }, TOAST_MS);
    } catch (e) {}
  }

  function bindCart() {
    document.addEventListener(
      "click",
      function (evt) {
        try {
          var el = evt.target.closest
            ? evt.target.closest('[data-ll-track-click="add_to_cart"]')
            : null;
          if (!el) return;
          var n = readCount() + 1;
          writeCount(n);
          paintCart(n);
          var label = (el.getAttribute("data-ll-label") || "Course").trim();
          toast("Added · " + (label.length > 42 ? label.slice(0, 42) + "…" : label));
          // Flush now rather than at the 5s tick: this is the highest-weighted
          // signal there is (6.0), and the panel below should show it while the
          // user is still looking at the page that produced it.
          if (window.LLTrack && window.LLTrack.flush) window.LLTrack.flush();
          setTimeout(refreshSignal, 900);
        } catch (e) {}
      },
      { passive: true, capture: true }
    );
  }

  // ---------------------------------------------------------------- filter
  /**
   * Emit the active catalog facet as a `search` event.
   *
   * Reuses `search` rather than a new event type on purpose: it is already in
   * MEANINGFUL_TYPES (so it moves the momentum gate), already weighted 3.0, and
   * already feeds recent_searches, which the agent turns into retrieval queries.
   * A new type would need changes in ingest, scoring and the agent to do the
   * same work. meta.source lets the panel label it "Filtered", not "Searched".
   */
  function trackFilter() {
    var el = document.querySelector("[data-ll-filter]");
    if (!el) return;
    var facet = (el.getAttribute("data-ll-filter") || "").trim();
    if (!facet) return;
    if (!window.LLTrack || !window.LLTrack.track) return;

    // Reloading or paging through the same filtered view is not new interest,
    // and counting it would inflate events_since_last_rec into a regeneration
    // the user never earned. Re-picking the category later still counts.
    try {
      var prev = JSON.parse(sessionStorage.getItem(FILTER_KEY) || "null");
      if (prev && prev.facet === facet && Date.now() - prev.at < FILTER_DEDUP_MS) return;
      sessionStorage.setItem(FILTER_KEY, JSON.stringify({ facet: facet, at: Date.now() }));
    } catch (e) {
      /* storage blocked - fire anyway rather than lose the signal */
    }

    window.LLTrack.track("search", {
      search_query: facet,
      meta: { source: "filter" }
    });
  }

  // ---------------------------------------------------------------- signal
  function chipNode(chip) {
    var li = document.createElement("li");
    li.className = "chip" + (chip.strong ? " chip-strong" : "");

    var verb = document.createElement("span");
    verb.className = "chip-verb";
    verb.textContent = chip.verb;
    li.appendChild(verb);

    if (chip.detail) {
      var detail = document.createElement("span");
      detail.className = "chip-detail";
      // textContent, not innerHTML: course titles and search queries are user
      // and catalog data going straight back into the DOM.
      detail.textContent = chip.detail;
      li.appendChild(detail);
    }

    var when = document.createElement("span");
    when.className = "chip-when";
    when.textContent = chip.when;
    li.appendChild(when);
    return li;
  }

  function renderSignal(events) {
    var list = document.querySelector("[data-ll-signal-list]");
    if (!list) return;

    if (!events || !events.length) {
      list.innerHTML = "";
      var empty = document.createElement("li");
      empty.className = "chip chip-empty";
      empty.textContent = "Nothing tracked yet — browse a course.";
      list.appendChild(empty);
      return;
    }

    // Skip the repaint when the newest event hasn't changed: this polls every
    // few seconds and rebuilding an identical list would fight text selection.
    var head = list.getAttribute("data-head");
    if (head && String(events[0].id) === head) return;
    list.setAttribute("data-head", String(events[0].id));

    var frag = document.createDocumentFragment();
    events.forEach(function (chip) {
      frag.appendChild(chipNode(chip));
    });
    list.innerHTML = "";
    list.appendChild(frag);
  }

  var inFlight = false;

  function refreshSignal() {
    if (inFlight) return;
    if (!document.querySelector("[data-ll-signal-list]")) return;
    // A backgrounded tab has nothing to show; polling it would just burn
    // requests, which is the same reason the tracker pauses dwell.
    if (document.visibilityState !== "visible") return;

    inFlight = true;
    fetch(SIGNAL_ENDPOINT, { credentials: "same-origin", headers: { Accept: "application/json" } })
      .then(function (r) {
        return r.ok ? r.json() : null;
      })
      .then(function (data) {
        if (data && data.events) renderSignal(data.events);
      })
      .catch(function () {})
      .then(function () {
        inFlight = false;
      });
  }

  function init() {
    try {
      paintCart(readCount());
      bindCart();
      trackFilter();

      if (document.querySelector("[data-ll-signal-list]")) {
        setInterval(refreshSignal, SIGNAL_POLL_MS);
        document.addEventListener("visibilitychange", function () {
          if (document.visibilityState === "visible") refreshSignal();
        });
      }
    } catch (e) {
      /* presentation only - never allowed to break the page */
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
