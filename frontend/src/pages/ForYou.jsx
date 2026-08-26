import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { Compass, RefreshCw, Sparkles, UserRound } from "lucide-react";
import { api } from "../lib/api";
import { useResource } from "../lib/useResource";
import { localTime } from "../lib/format";
import { useAuth } from "../state/AuthContext";
import ProductCard from "../components/ProductCard";
import SignalPanel from "../components/SignalPanel";
import Empty from "../components/Empty";
import { StripSkeleton } from "../components/Skeletons";

/**
 * "For you" - the output of the agent, and the page the whole build exists for.
 *
 * Three deliberate choices:
 *
 *   - The recommendation gets the ink band, the same surface the homepage uses to
 *     explain the system. Dark is the system's voice throughout the site, so this
 *     page reads as the store talking rather than as another catalog grid.
 *   - Every card shows its `reason` as a visible annotation. That line is the
 *     entire point of the agent; behind a tooltip it may as well not exist.
 *   - Refresh runs the trigger gates server-side and only *queues* work. This
 *     page therefore polls for a new `generated_at` rather than awaiting a model
 *     call, and it reports the gate's own reason when nothing was queued.
 */

const POLL_MS = 3500;
const POLL_TRIES = 8;

function Meta({ recommendation }) {
  return (
    <div className="border border-rule bg-paper p-6">
      <h2 className="eyebrow !text-ink">How this was made</h2>
      <dl className="mt-4 space-y-3 text-[0.8125rem]">
        <div>
          <dt className="text-stone">Model</dt>
          <dd className="mt-0.5">
            {recommendation.model || "—"}
            {recommendation.is_fallback && (
              <span className="ml-2 pill border-terracotta text-terracotta">fallback</span>
            )}
          </dd>
        </div>
        {recommendation.trigger_reason && (
          <div>
            <dt className="text-stone">Regenerated because</dt>
            <dd className="mt-0.5">{recommendation.trigger_reason}</dd>
          </div>
        )}
        {recommendation.generated_at && (
          <div>
            <dt className="text-stone">Generated</dt>
            <dd className="mt-0.5 tabular-nums">{localTime(recommendation.generated_at)}</dd>
          </div>
        )}
      </dl>
      {recommendation.is_fallback && (
        <p className="mt-4 border-t border-rule pt-4 text-[0.75rem] leading-relaxed text-stone">
          The model was unavailable or its answer failed grounding, so this edit came from the
          deterministic fallback: retrieval and rerank without generated prose.
        </p>
      )}
    </div>
  );
}

export default function ForYou() {
  const { user, ready } = useAuth();
  const [fresh, setFresh] = useState(null);
  const [refreshing, setRefreshing] = useState(false);
  const [notice, setNotice] = useState("");
  const alive = useRef(true);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const { data, loading, error } = useResource(
    (signal) => (user ? api.recommendations(signal) : Promise.resolve(null)),
    [user?.id]
  );

  const payload = fresh || data;
  const recommendation = payload?.recommendation;

  const refresh = useCallback(async () => {
    setRefreshing(true);
    setNotice("");
    const before = recommendation?.generated_at || null;

    try {
      const result = await api.refreshRecommendations();
      if (!alive.current) return;

      if (!result.queued) {
        // The gate declined, and its reason is the honest answer: usually "not
        // enough new signal", which is a real answer and not an error.
        setNotice(result.decision || "No refresh needed yet.");
        setRefreshing(false);
        return;
      }

      setNotice("The agent is working on a new edit…");

      let tries = 0;
      const poll = async () => {
        if (!alive.current) return;
        tries += 1;
        try {
          const next = await api.recommendations();
          if (!alive.current) return;
          if (next?.recommendation?.generated_at && next.recommendation.generated_at !== before) {
            setFresh(next);
            setRefreshing(false);
            setNotice("Fresh edit, based on what you have been looking at.");
            return;
          }
        } catch {
          /* keep polling - a single failed read is not the end of the attempt */
        }
        if (tries >= POLL_TRIES) {
          setRefreshing(false);
          setNotice("Still generating. It will be here on your next visit.");
          return;
        }
        setTimeout(poll, POLL_MS);
      };
      setTimeout(poll, POLL_MS);
    } catch (failure) {
      if (!alive.current) return;
      setNotice(failure.message);
      setRefreshing(false);
    }
  }, [recommendation?.generated_at]);

  // ---------------------------------------------------------------- gates
  if (!ready) {
    return (
      <div className="mx-auto max-w-[1400px] px-5 py-20">
        <div className="skeleton h-64 w-full" />
      </div>
    );
  }

  if (!user) {
    return (
      <Empty
        icon={UserRound}
        title="Your edit needs an account"
        body="Recommendations are built from your own browsing, so they live behind a sign-in. Nothing is shared between accounts, and the signal is read from your session cookie rather than anything the page could pass in."
        action="Sign in"
        actionTo="/login"
      >
        <Link to="/register" className="link-rule mt-4 text-[0.8125rem] text-stone">
          or create an account
        </Link>
      </Empty>
    );
  }

  if (error && !recommendation) {
    return (
      <Empty
        icon={Sparkles}
        title="Could not load your edit"
        body={error.message}
        action="Browse the catalog"
        actionTo="/shop"
      />
    );
  }

  const pending = !loading && payload && payload.status !== "ok";

  return (
    <div>
      {/* ---------------- The band: the system speaking ---------------- */}
      <section className="bg-ink text-bone">
        <div className="mx-auto max-w-[1400px] px-5 py-16 lg:py-20">
          <div className="flex flex-wrap items-start justify-between gap-8">
            <div className="max-w-2xl">
              <p className="eyebrow !text-terracotta flex items-center gap-2">
                <Sparkles size={12} strokeWidth={1.5} />
                {recommendation ? "Recommended for you" : "Your edit"}
              </p>

              {loading && !payload ? (
                <>
                  <div className="skeleton mt-5 h-10 w-96 max-w-full opacity-20" />
                  <div className="skeleton mt-4 h-4 w-full max-w-xl opacity-20" />
                </>
              ) : recommendation ? (
                <>
                  <h1 className="font-display mt-4 text-3xl leading-tight sm:text-[2.75rem]">
                    {recommendation.headline}
                  </h1>
                  {recommendation.narrative && (
                    <p className="mt-5 text-[0.9375rem] leading-relaxed text-bone/70">
                      {recommendation.narrative}
                    </p>
                  )}
                  {recommendation.detected_interests?.length > 0 && (
                    <div className="mt-7">
                      <p className="eyebrow !text-bone/45">Read from your behaviour</p>
                      <div className="mt-3 flex flex-wrap gap-2">
                        {recommendation.detected_interests.map((interest) => (
                          <span
                            key={interest}
                            className="pill border-bone/25 text-bone/85"
                          >
                            {interest}
                          </span>
                        ))}
                      </div>
                    </div>
                  )}
                </>
              ) : (
                <>
                  <h1 className="font-display mt-4 text-3xl leading-tight sm:text-[2.75rem]">
                    Nothing to show you
                    <span className="italic"> yet.</span>
                  </h1>
                  <p className="mt-5 text-[0.9375rem] leading-relaxed text-bone/70">
                    The agent will not guess. It waits until there is enough behaviour to read —
                    a few products opened, a search, something added to your bag — and then writes
                    an edit explaining what it noticed. Browse for a minute and watch the signal
                    panel below fill up.
                  </p>
                </>
              )}
            </div>

            <div className="flex flex-col items-start gap-3">
              <button
                type="button"
                onClick={refresh}
                disabled={refreshing}
                className="btn btn-primary disabled:opacity-60"
              >
                <RefreshCw
                  size={14}
                  strokeWidth={1.75}
                  className={refreshing ? "animate-spin" : ""}
                />
                {refreshing ? "Working" : "Refresh my edit"}
              </button>
              <p className="max-w-[16rem] text-[0.6875rem] leading-relaxed text-bone/45">
                Refreshing still runs the trigger gates — a button that bypassed them would be a
                free way to burn the model budget.
              </p>
            </div>
          </div>

          {notice && (
            <p className="mt-8 border-l-2 border-terracotta pl-4 text-[0.8125rem] text-bone/80">
              {notice}
            </p>
          )}
        </div>
      </section>

      {/* ---------------- Items and signal ---------------- */}
      <div className="mx-auto grid max-w-[1400px] gap-12 px-5 py-14 lg:grid-cols-12 lg:gap-12">
        <div className="lg:col-span-8 xl:col-span-9">
          {loading && !payload ? (
            <StripSkeleton count={4} />
          ) : recommendation?.items?.length ? (
            <>
              <div className="flex items-baseline justify-between border-b border-rule pb-5">
                <h2 className="font-display text-2xl">
                  {recommendation.items.length} pieces, and why
                </h2>
                <Link to="/shop" className="link-rule pb-1 text-[0.8125rem]">
                  Browse everything
                </Link>
              </div>
              <div className="mt-8 grid grid-cols-1 gap-x-6 gap-y-10 sm:grid-cols-2">
                {recommendation.items.map((item) => (
                  <ProductCard
                    key={item.product.id}
                    product={item.product}
                    rank={item.rank}
                    reason={item.reason}
                  />
                ))}
              </div>
            </>
          ) : (
            <div className="border border-rule bg-paper">
              <Empty
                icon={Compass}
                title={pending ? "No edit yet" : "This edit came back empty"}
                body={
                  pending
                    ? "Open a few products and the agent will have something to work with. It needs only a handful of events, and dwell time counts for more than a click."
                    : "The recommendation exists but none of its products are still published."
                }
                action="Start browsing"
                actionTo="/shop"
              />
            </div>
          )}
        </div>

        <aside className="space-y-6 lg:col-span-4 xl:col-span-3">
          <SignalPanel />
          {recommendation && <Meta recommendation={recommendation} />}
        </aside>
      </div>
    </div>
  );
}
