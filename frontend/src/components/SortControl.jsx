import { SORT_LABELS } from "../lib/format";
import { ChevronDown } from "lucide-react";

/**
 * Sort control.
 *
 * The options come from `/api/catalog/facets`, not from a list hardcoded here,
 * so the dropdown cannot offer a key the catalog endpoint would silently fall
 * back on. A native select on purpose: it is the one control where the OS
 * picker beats anything custom on a phone.
 */
export default function SortControl({ value, options = [], onChange }) {
  return (
    <div className="relative">
      <label htmlFor="sort" className="sr-only">
        Sort products
      </label>
      <select
        id="sort"
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="appearance-none border-b border-rule-dark bg-transparent py-1.5 pr-7 pl-0 text-[0.8125rem] focus:border-ink focus:outline-none"
      >
        {options.map((option) => (
          <option key={option} value={option}>
            {SORT_LABELS[option] || option}
          </option>
        ))}
      </select>
      <ChevronDown
        size={14}
        strokeWidth={1.5}
        className="pointer-events-none absolute top-1/2 right-1 -translate-y-1/2 text-stone"
      />
    </div>
  );
}
