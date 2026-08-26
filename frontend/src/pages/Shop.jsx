import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { SearchX, SlidersHorizontal, X } from "lucide-react";
import { api } from "../lib/api";
import { useResource } from "../lib/useResource";
import { useBodyLock, useEscape } from "../lib/useBodyLock";
import { trackFilter } from "../lib/tracker";
import { money, plural } from "../lib/format";
import ProductCard from "../components/ProductCard";
import FilterSidebar from "../components/FilterSidebar";
import SortControl from "../components/SortControl";
import Pagination from "../components/Pagination";
import Empty from "../components/Empty";
import { GridSkeleton } from "../components/Skeletons";

/**
 * The catalog.
 *
 * All filter state lives in the URL rather than in component state, which is
 * what makes a filtered view shareable, survivable across a reload, and correct
 * under the back button. It also means one thing owns the request: the query
 * string is the dependency the fetch watches.
 *
 * The facets request is separate and unfiltered, so changing a category does not
 * re-fetch the sidebar and make the counts flicker.
 */

const EMPTY = {
  q: "",
  category: "",
  brand: "",
  min_price: "",
  max_price: "",
  min_rating: "",
};

export default function Shop() {
  const [searchParams, setSearchParams] = useSearchParams();
  const [filtersOpen, setFiltersOpen] = useState(false);

  useBodyLock(filtersOpen);
  useEscape(filtersOpen, () => setFiltersOpen(false));

  const params = useMemo(
    () => ({
      q: searchParams.get("q") || "",
      category: searchParams.get("category") || "",
      brand: searchParams.get("brand") || "",
      min_price: searchParams.get("min_price") || "",
      max_price: searchParams.get("max_price") || "",
      min_rating: searchParams.get("min_rating") || "",
      sort: searchParams.get("sort") || "featured",
      page: Number(searchParams.get("page")) || 1,
    }),
    [searchParams]
  );

  const key = searchParams.toString();
  const { data, loading, error } = useResource((signal) => api.catalog(params, signal), [key]);
  const { data: facets } = useResource((signal) => api.facets(signal), []);

  /**
   * Record the active facet as a search event.
   *
   * Category and brand only: those are the two selections that mean something as
   * a retrieval query, which is what the agent turns `recent_searches` into. A
   * price ceiling or a star threshold would just be noise in that query. The
   * typed `q` is excluded because the search box fires its own event - including
   * it here would count one search twice.
   */
  useEffect(() => {
    trackFilter([params.category, params.brand]);
  }, [params.category, params.brand]);

  const update = (patch) => {
    const next = new URLSearchParams(searchParams);
    Object.entries(patch).forEach(([field, value]) => {
      if (value === "" || value === null || value === undefined) next.delete(field);
      else next.set(field, String(value));
    });
    // Any change other than paging returns to page one: page 4 of a narrower
    // result set is usually empty, and the server would clamp it anyway.
    if (!("page" in patch)) next.delete("page");
    setSearchParams(next);
  };

  const clearAll = () => setSearchParams(params.sort === "featured" ? {} : { sort: params.sort });

  const products = data?.products || [];
  const chips = [
    params.q && { label: `“${params.q}”`, clear: { q: "" } },
    params.category && { label: params.category, clear: { category: "" } },
    params.brand && { label: params.brand, clear: { brand: "" } },
    (params.min_price || params.max_price) && {
      label: `${params.min_price ? money(params.min_price) : money(facets?.price?.min ?? 0)} – ${
        params.max_price ? money(params.max_price) : money(facets?.price?.max ?? 0)
      }`,
      clear: { min_price: "", max_price: "" },
    },
    params.min_rating && { label: `${params.min_rating}★ and up`, clear: { min_rating: "" } },
  ].filter(Boolean);

  const sidebar = (
    <FilterSidebar
      facets={facets}
      params={params}
      onChange={(patch) => {
        update(patch);
        setFiltersOpen(false);
      }}
      onClear={() => {
        clearAll();
        setFiltersOpen(false);
      }}
      activeCount={chips.length}
    />
  );

  return (
    <div className="mx-auto max-w-[1400px] px-5 pb-24">
      <header className="border-b border-rule py-10">
        <p className="eyebrow">{params.q ? "Search" : params.category || "Catalog"}</p>
        <h1 className="font-display mt-3 text-3xl sm:text-[2.5rem]">
          {params.q ? `“${params.q}”` : params.category || "Everything"}
        </h1>
        <p className="mt-3 text-sm text-stone">
          {data ? (
            <>
              {plural(data.total, "piece")}
              {chips.length > 0 && facets ? ` of ${facets.total}` : ""}
            </>
          ) : (
            <span className="text-stone-light">Counting…</span>
          )}
        </p>
      </header>

      <div className="flex flex-wrap items-center justify-between gap-4 border-b border-rule py-4">
        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={() => setFiltersOpen(true)}
            className="btn btn-outline py-2 lg:hidden"
          >
            <SlidersHorizontal size={14} strokeWidth={1.5} />
            Filter{chips.length > 0 ? ` (${chips.length})` : ""}
          </button>

          {chips.map((chip) => (
            <button
              key={chip.label}
              type="button"
              onClick={() => update(chip.clear)}
              className="pill flex items-center gap-1.5 border-ink text-ink hover:border-terracotta hover:text-terracotta"
            >
              {chip.label}
              <X size={12} strokeWidth={2} />
            </button>
          ))}
          {chips.length > 1 && (
            <button type="button" onClick={clearAll} className="link-rule ml-1 text-[0.75rem] text-stone">
              Clear all
            </button>
          )}
        </div>

        <SortControl
          value={params.sort}
          // Until facets land, the select carries only the sort that is actually
          // in effect - an empty option list would leave a controlled select with
          // a value nothing matches.
          options={facets?.sorts || [params.sort]}
          onChange={(sort) => update({ sort })}
        />
      </div>

      <div className="grid gap-10 pt-10 lg:grid-cols-12 lg:gap-12">
        <aside className="hidden lg:col-span-3 lg:block">
          <div className="sticky top-28">{sidebar}</div>
        </aside>

        <div className="lg:col-span-9">
          {error ? (
            <Empty
              icon={SearchX}
              title="Could not load the catalog"
              body={error.message}
              action="Clear filters"
              onAction={clearAll}
            />
          ) : loading && !products.length ? (
            // 12 mirrors the API's PAGE_SIZE, so the skeleton occupies the same
            // number of rows the results will - a count of 9 made the page jump a
            // row taller the moment it loaded.
            <GridSkeleton count={12} />
          ) : products.length === 0 ? (
            <Empty
              icon={SearchX}
              title="Nothing matched"
              body={
                params.q
                  ? `No piece in the catalog mentions “${params.q}”. Try a broader word, or drop a filter.`
                  : "That combination of filters is too narrow. Try widening the price range or clearing a facet."
              }
              action="Clear filters"
              onAction={clearAll}
            />
          ) : (
            <>
              <div
                className={`grid grid-cols-2 gap-x-5 gap-y-10 lg:grid-cols-3 ${
                  loading ? "opacity-60 transition-opacity" : ""
                }`}
              >
                {products.map((product) => (
                  <ProductCard key={product.id} product={product} />
                ))}
              </div>

              <Pagination
                page={data.page}
                pages={data.pages}
                hasNext={data.has_next}
                hasPrev={data.has_prev}
                onChange={(page) => {
                  update({ page });
                  window.scrollTo({ top: 0, behavior: "smooth" });
                }}
              />
            </>
          )}
        </div>
      </div>

      {/* Mobile filters. A sheet rather than an accordion above the grid: the
          facet list is long enough that inline expansion would push the products
          off the screen entirely. */}
      {filtersOpen && (
        <div className="fixed inset-0 z-50 lg:hidden">
          <div
            className="absolute inset-0 bg-ink/30 backdrop-blur-[2px]"
            onClick={() => setFiltersOpen(false)}
          />
          <div className="absolute inset-y-0 left-0 flex w-[88%] max-w-sm flex-col bg-bone shadow-lift">
            <div className="flex items-center justify-between border-b border-rule px-5 py-4">
              <span className="font-display text-lg">Filter</span>
              <button type="button" onClick={() => setFiltersOpen(false)} aria-label="Close filters">
                <X size={20} strokeWidth={1.5} />
              </button>
            </div>
            <div className="flex-1 overflow-y-auto px-5 py-5">{sidebar}</div>
          </div>
        </div>
      )}
    </div>
  );
}
