import { useEffect, useState } from "react";
import { Check } from "lucide-react";
import { money } from "../lib/format";

/**
 * Catalog filters: category, brand, price, rating.
 *
 * Counts and bounds come from `/api/catalog/facets`, so the price fields end
 * where the catalog does rather than at a number typed into this file. The
 * facet counts are deliberately not filter-aware on the server, which is why
 * they never change as selections are made - a stable count reads better than
 * "Apparel (0)" appearing the moment you pick Footwear.
 *
 * Price is applied on submit rather than per keystroke: a debounce on a number
 * field fires a request for "1" and "14" on the way to "140", and each of those
 * is a page of results the user never asked for.
 */

const RATINGS = [
  { value: "", label: "Any rating" },
  { value: "4.5", label: "4.5 and up" },
  { value: "4", label: "4.0 and up" },
  { value: "3.5", label: "3.5 and up" },
];

function Section({ title, children }) {
  return (
    <div className="border-b border-rule py-6 first:pt-0">
      <p className="eyebrow">{title}</p>
      <div className="mt-4">{children}</div>
    </div>
  );
}

function Option({ active, count, children, onClick }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`flex w-full items-center justify-between gap-3 py-1.5 text-left text-[0.8125rem] transition-colors ${
        active ? "text-terracotta" : "text-stone hover:text-ink"
      }`}
    >
      <span className="flex min-w-0 items-center gap-2">
        <span className="w-3.5 shrink-0">
          {active && <Check size={13} strokeWidth={2} />}
        </span>
        <span className="truncate">{children}</span>
      </span>
      {count !== undefined && (
        <span className="shrink-0 text-[0.6875rem] text-stone-light tabular-nums">{count}</span>
      )}
    </button>
  );
}

export default function FilterSidebar({ facets, params, onChange, onClear, activeCount }) {
  const bounds = facets?.price || { min: 0, max: 0 };
  const [minPrice, setMinPrice] = useState(params.min_price || "");
  const [maxPrice, setMaxPrice] = useState(params.max_price || "");

  // Reset when the URL changes underneath us - a category link from the footer,
  // or the browser's back button, both land here without touching these fields.
  useEffect(() => {
    setMinPrice(params.min_price || "");
    setMaxPrice(params.max_price || "");
  }, [params.min_price, params.max_price]);

  const toggle = (key, value) => onChange({ [key]: params[key] === value ? "" : value });

  return (
    <div>
      <div className="flex items-baseline justify-between pb-5">
        <p className="font-display text-lg">Filter</p>
        {activeCount > 0 && (
          <button type="button" onClick={onClear} className="link-rule text-[0.75rem] text-stone">
            Clear all ({activeCount})
          </button>
        )}
      </div>

      <Section title="Category">
        <Option active={!params.category} onClick={() => onChange({ category: "" })} count={facets?.total}>
          All categories
        </Option>
        {(facets?.categories || []).map((facet) => (
          <Option
            key={facet.value}
            active={params.category === facet.value}
            count={facet.count}
            onClick={() => toggle("category", facet.value)}
          >
            {facet.value}
          </Option>
        ))}
      </Section>

      <Section title="Brand">
        <Option active={!params.brand} onClick={() => onChange({ brand: "" })}>
          All brands
        </Option>
        {(facets?.brands || []).map((facet) => (
          <Option
            key={facet.value}
            active={params.brand === facet.value}
            count={facet.count}
            onClick={() => toggle("brand", facet.value)}
          >
            {facet.value}
          </Option>
        ))}
      </Section>

      <Section title="Price">
        <p className="mb-3 text-[0.75rem] text-stone-light">
          Catalog runs {money(bounds.min)} – {money(bounds.max)}
        </p>
        <form
          onSubmit={(event) => {
            event.preventDefault();
            onChange({ min_price: minPrice, max_price: maxPrice });
          }}
          className="flex items-center gap-2"
        >
          <label className="sr-only" htmlFor="min-price">
            Minimum price
          </label>
          <input
            id="min-price"
            type="number"
            min={0}
            step={1}
            inputMode="numeric"
            value={minPrice}
            onChange={(event) => setMinPrice(event.target.value)}
            placeholder={String(Math.floor(bounds.min))}
            className="field py-2 text-[0.8125rem] tabular-nums"
          />
          <span className="text-stone-light">–</span>
          <label className="sr-only" htmlFor="max-price">
            Maximum price
          </label>
          <input
            id="max-price"
            type="number"
            min={0}
            step={1}
            inputMode="numeric"
            value={maxPrice}
            onChange={(event) => setMaxPrice(event.target.value)}
            placeholder={String(Math.ceil(bounds.max))}
            className="field py-2 text-[0.8125rem] tabular-nums"
          />
          <button type="submit" className="btn btn-quiet shrink-0 px-3 py-2 text-[0.6875rem]">
            Go
          </button>
        </form>
      </Section>

      <Section title="Rating">
        <div className="flex flex-wrap gap-2">
          {RATINGS.map((option) => {
            const active = (params.min_rating || "") === option.value;
            return (
              <button
                key={option.label}
                type="button"
                onClick={() => onChange({ min_rating: option.value })}
                className={`pill transition-colors ${
                  active
                    ? "border-terracotta bg-terracotta text-bone"
                    : "hover:border-ink hover:text-ink"
                }`}
              >
                {option.label}
              </button>
            );
          })}
        </div>
      </Section>
    </div>
  );
}
