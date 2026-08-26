import { ChevronLeft, ChevronRight } from "lucide-react";

/**
 * Numbered pagination.
 *
 * Pages, not infinite scroll: the catalog is browsable in a few screens, and an
 * infinite list has no footer, no shareable position, and no way back to where
 * you were after opening a product.
 *
 * `page`, `pages`, `has_next` and `has_prev` all come from the response rather
 * than being derived here, because the server clamps what it was asked for - a
 * client that re-derived them from `total` would draw controls for a page the
 * API had already declined to serve.
 */

/** Up to 7 slots: first, last, the neighbourhood of the current page, ellipses. */
function pageList(page, pages) {
  if (pages <= 7) return Array.from({ length: pages }, (_, i) => i + 1);

  const slots = new Set([1, pages, page, page - 1, page + 1]);
  if (page <= 3) [2, 3, 4].forEach((n) => slots.add(n));
  if (page >= pages - 2) [pages - 3, pages - 2, pages - 1].forEach((n) => slots.add(n));

  const sorted = [...slots].filter((n) => n >= 1 && n <= pages).sort((a, b) => a - b);
  const withGaps = [];
  sorted.forEach((n, i) => {
    if (i > 0 && n - sorted[i - 1] > 1) withGaps.push(`gap-${n}`);
    withGaps.push(n);
  });
  return withGaps;
}

export default function Pagination({ page, pages, hasNext, hasPrev, onChange }) {
  if (pages <= 1) return null;

  return (
    <nav className="mt-16 flex items-center justify-center gap-1" aria-label="Pagination">
      <button
        type="button"
        disabled={!hasPrev}
        onClick={() => onChange(page - 1)}
        className="flex h-9 items-center gap-1 px-3 text-[0.8125rem] text-stone disabled:opacity-30 enabled:hover:text-ink"
        aria-label="Previous page"
      >
        <ChevronLeft size={15} strokeWidth={1.5} />
        <span className="hidden sm:inline">Prev</span>
      </button>

      {pageList(page, pages).map((slot) =>
        typeof slot === "string" ? (
          <span key={slot} className="px-1 text-stone-light">
            …
          </span>
        ) : (
          <button
            key={slot}
            type="button"
            onClick={() => onChange(slot)}
            aria-current={slot === page ? "page" : undefined}
            className={`h-9 min-w-9 px-1 text-[0.8125rem] tabular-nums transition-colors ${
              slot === page
                ? "border-b border-ink font-medium text-ink"
                : "text-stone hover:text-ink"
            }`}
          >
            {slot}
          </button>
        )
      )}

      <button
        type="button"
        disabled={!hasNext}
        onClick={() => onChange(page + 1)}
        className="flex h-9 items-center gap-1 px-3 text-[0.8125rem] text-stone disabled:opacity-30 enabled:hover:text-ink"
        aria-label="Next page"
      >
        <span className="hidden sm:inline">Next</span>
        <ChevronRight size={15} strokeWidth={1.5} />
      </button>
    </nav>
  );
}
