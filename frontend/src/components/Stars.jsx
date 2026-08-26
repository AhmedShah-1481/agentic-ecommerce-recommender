import { Star } from "lucide-react";
import { rating as formatRating } from "../lib/format";

/**
 * A rating, drawn as five glyphs with the last one clipped to the fraction.
 *
 * The clip is a nested span with a percentage width rather than a half-star
 * icon, so a 4.3 looks like 4.3 instead of rounding to 4.5 in the one place a
 * shopper is comparing two products closely.
 */
export default function Stars({ value = 0, size = 12, showValue = true, className = "" }) {
  const score = Math.max(0, Math.min(5, Number(value) || 0));
  const pct = `${(score / 5) * 100}%`;

  return (
    <span className={`inline-flex items-center gap-1.5 ${className}`}>
      <span
        className="relative inline-flex"
        role="img"
        aria-label={`Rated ${formatRating(score)} out of 5`}
      >
        <span className="flex text-rule-dark">
          {[0, 1, 2, 3, 4].map((i) => (
            <Star key={i} size={size} strokeWidth={1.25} />
          ))}
        </span>
        <span
          className="absolute inset-y-0 left-0 flex overflow-hidden text-terracotta"
          style={{ width: pct }}
        >
          {[0, 1, 2, 3, 4].map((i) => (
            <Star key={i} size={size} strokeWidth={1.25} fill="currentColor" className="shrink-0" />
          ))}
        </span>
      </span>
      {showValue && (
        <span className="text-[0.6875rem] text-stone tabular-nums">{formatRating(score)}</span>
      )}
    </span>
  );
}
