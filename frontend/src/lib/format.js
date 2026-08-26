/** Formatting shared across the storefront, so a price looks the same everywhere. */

const MONEY = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  // Catalog prices are whole or .00 dollars; trailing zeros on every card is
  // noise. A genuine 24.50 still shows its cents.
  minimumFractionDigits: 0,
  maximumFractionDigits: 2,
});

export const money = (value) => MONEY.format(Number(value) || 0);

export const rating = (value) => (Number(value) || 0).toFixed(1);

/** "3 items" / "1 item" - the count is almost always adjacent to the noun. */
export const plural = (count, one, many = `${one}s`) =>
  `${count} ${count === 1 ? one : many}`;

/** Sort keys come from the API; these are their labels. */
export const SORT_LABELS = {
  featured: "Featured",
  price_asc: "Price, low to high",
  price_desc: "Price, high to low",
  rating: "Top rated",
  newest: "Newest",
};

/**
 * An API timestamp as local time.
 *
 * The server stores UTC in a naive column, so `.isoformat()` comes back without
 * an offset - and JavaScript reads an offsetless string as *local* time, which
 * would silently mislabel every timestamp by the viewer's UTC offset. Appending
 * "Z" when no zone is present tells Date the truth.
 */
export function localTime(iso) {
  if (!iso) return "";
  const zoned = /[Z+]|-\d\d:\d\d$/.test(iso) ? iso : `${iso}Z`;
  const date = new Date(zoned);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}
