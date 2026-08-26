import { useEffect, useRef, useState } from "react";
import { Activity } from "lucide-react";
import { api } from "../lib/api";
import { SignalSkeleton } from "./Skeletons";

/**
 * "Your signal" - the live view of what the tracker is recording.
 *
 * This panel is the only place the behavioural pipeline is visible. Everything
 * else about it is invisible by design (batched writes, a background consumer,
 * gates that suppress most regenerations), which means without this the system
 * looks like it is doing nothing until a recommendation appears minutes later.
 *
 * Three things keep the poll cheap, all carried over from the server-rendered
 * version:
 *
 *   - 4s interval, which is fast enough to feel live while browsing.
 *   - Skipped entirely while the tab is hidden. A background tab polling every
 *     4s all afternoon is the kind of thing that gets a demo throttled.
 *   - No repaint unless the newest event id changed. The response is identical
 *     most of the time, and re-rendering identical chips would restart their
 *     enter animation every four seconds.
 *
 * The endpoint is scoped to the session cookie server-side, so there is no user
 * id to pass and no way to ask for someone else's history.
 */
const POLL_MS = 4000;

export default function SignalPanel({ limit = 8 }) {
  const [events, setEvents] = useState(null);
  const newestRef = useRef(null);

  useEffect(() => {
    let live = true;
    const controller = new AbortController();

    const poll = async () => {
      if (document.visibilityState !== "visible") return;
      try {
        const data = await api.signal(limit, controller.signal);
        if (!live) return;
        const newest = data?.events?.[0]?.id ?? null;
        // Nothing new: leave the DOM exactly as it is.
        if (newest === newestRef.current && events !== null) return;
        newestRef.current = newest;
        setEvents(data?.events || []);
      } catch (error) {
        if (error.name === "AbortError") return;
        // A 401 here means the session ended in another tab. An empty panel is
        // the honest answer; the page around it handles the sign-in prompt.
        if (live) setEvents([]);
      }
    };

    poll();
    const id = setInterval(poll, POLL_MS);
    const onVisible = () => {
      if (document.visibilityState === "visible") poll();
    };
    document.addEventListener("visibilitychange", onVisible);

    return () => {
      live = false;
      controller.abort();
      clearInterval(id);
      document.removeEventListener("visibilitychange", onVisible);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [limit]);

  return (
    <section className="border border-rule bg-paper p-6">
      <div className="flex items-center gap-2">
        <Activity size={15} strokeWidth={1.5} className="text-terracotta" />
        <h2 className="eyebrow !text-ink">Your signal</h2>
        <span className="relative ml-auto flex h-1.5 w-1.5">
          <span className="absolute inline-flex h-full w-full animate-ping rounded-pill bg-terracotta opacity-60" />
          <span className="relative inline-flex h-1.5 w-1.5 rounded-pill bg-terracotta" />
        </span>
      </div>

      <p className="mt-3 text-[0.75rem] leading-relaxed text-stone">
        Live from the events table. The number is the weight this action carries
        when your profile is scored.
      </p>

      <div className="mt-5">
        {events === null ? (
          <SignalSkeleton />
        ) : events.length === 0 ? (
          <p className="text-[0.8125rem] leading-relaxed text-stone-light">
            Nothing recorded yet. Open a product, search for something, or add to your bag —
            chips appear here within a few seconds.
          </p>
        ) : (
          <ul className="space-y-2">
            {events.map((event) => (
              <li
                key={event.id}
                className={`flex items-baseline gap-2 border-l-2 py-1.5 pl-3 text-[0.8125rem] ${
                  event.strong ? "border-terracotta" : "border-rule-dark"
                }`}
              >
                <span className={event.strong ? "font-medium" : ""}>{event.verb}</span>
                {event.detail && (
                  <span className="min-w-0 flex-1 truncate text-stone">{event.detail}</span>
                )}
                <span className="ml-auto shrink-0 text-[0.6875rem] text-stone-light tabular-nums">
                  ×{event.weight} · {event.when}
                </span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </section>
  );
}
