/**
 * Loading skeletons.
 *
 * Skeletons rather than a spinner, and specifically skeletons shaped like the
 * content they stand in for: the grid's real layout is on screen from the first
 * frame, so nothing reflows when the data lands. A spinner in the middle of an
 * empty page tells the user to wait; this tells them what they are waiting for.
 */

export function CardSkeleton() {
  return (
    <div className="flex flex-col">
      <div className="skeleton aspect-4/5 w-full rounded-card" />
      <div className="skeleton mt-4 h-2 w-16 rounded-pill" />
      <div className="skeleton mt-3 h-3.5 w-4/5 rounded-pill" />
      <div className="mt-3 flex items-center justify-between">
        <div className="skeleton h-3 w-12 rounded-pill" />
        <div className="skeleton h-3 w-16 rounded-pill" />
      </div>
    </div>
  );
}

export function GridSkeleton({ count = 12, className = "" }) {
  return (
    <div
      className={`grid grid-cols-2 gap-x-5 gap-y-10 md:grid-cols-3 xl:grid-cols-4 ${className}`}
      aria-hidden="true"
    >
      {Array.from({ length: count }, (_, i) => (
        <CardSkeleton key={i} />
      ))}
    </div>
  );
}

export function StripSkeleton({ count = 4 }) {
  return (
    <div className="grid grid-cols-2 gap-x-5 gap-y-10 lg:grid-cols-4" aria-hidden="true">
      {Array.from({ length: count }, (_, i) => (
        <CardSkeleton key={i} />
      ))}
    </div>
  );
}

/** The product page: panel on the left, copy stack on the right. */
export function DetailSkeleton() {
  return (
    <div className="grid gap-10 lg:grid-cols-2 lg:gap-16" aria-hidden="true">
      <div>
        <div className="skeleton aspect-4/5 w-full rounded-card" />
        <div className="mt-3 grid grid-cols-4 gap-3">
          {Array.from({ length: 4 }, (_, i) => (
            <div key={i} className="skeleton aspect-square rounded-card" />
          ))}
        </div>
      </div>
      <div className="lg:pt-6">
        <div className="skeleton h-2 w-20 rounded-pill" />
        <div className="skeleton mt-5 h-8 w-11/12 rounded-pill" />
        <div className="skeleton mt-3 h-8 w-2/3 rounded-pill" />
        <div className="skeleton mt-7 h-4 w-24 rounded-pill" />
        <div className="skeleton mt-8 h-px w-full" />
        <div className="mt-8 space-y-3">
          <div className="skeleton h-3 w-full rounded-pill" />
          <div className="skeleton h-3 w-full rounded-pill" />
          <div className="skeleton h-3 w-4/5 rounded-pill" />
        </div>
        <div className="skeleton mt-10 h-12 w-full rounded-card" />
      </div>
    </div>
  );
}

/** The signal panel's chips, which arrive on a poll. */
export function SignalSkeleton({ count = 5 }) {
  return (
    <ul className="space-y-2.5" aria-hidden="true">
      {Array.from({ length: count }, (_, i) => (
        <li key={i} className="skeleton h-9 rounded-card" />
      ))}
    </ul>
  );
}
