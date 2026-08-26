import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { Minus, PackageSearch, Plus, RotateCcw, ShieldCheck, Truck } from "lucide-react";
import { api } from "../lib/api";
import { useResource } from "../lib/useResource";
import { money } from "../lib/format";
import { VARIANTS } from "../lib/productVisual";
import { useCart } from "../state/CartContext";
import ProductVisual from "../components/ProductVisual";
import ProductCard from "../components/ProductCard";
import Stars from "../components/Stars";
import Empty from "../components/Empty";
import { DetailSkeleton, StripSkeleton } from "../components/Skeletons";

/**
 * Product detail.
 *
 * The `product_view` event and the dwell timer for this page are not started
 * here - `RouteTracker` reads the id out of the path and calls `startPage` for
 * every navigation, so the page cannot forget to be instrumented and cannot
 * double-count when React re-mounts it.
 *
 * The "gallery" is four compositions of the same generated panel rather than
 * four photographs. It is not pretending to be a photo carousel: the caption
 * says what it is, and the thumbnails give the layout the rhythm a real gallery
 * has without inventing product imagery the catalog does not ship.
 */

const NOTES = [
  { Icon: Truck, text: "Complimentary shipping over $150" },
  { Icon: RotateCcw, text: "30 days to change your mind" },
  { Icon: ShieldCheck, text: "Two-year maker's warranty" },
];

function Spec({ label, children }) {
  if (!children) return null;
  return (
    <div className="flex justify-between gap-6 border-b border-rule py-2.5">
      <dt className="text-stone">{label}</dt>
      <dd className="text-right">{children}</dd>
    </div>
  );
}

export default function ProductDetail() {
  const { id } = useParams();
  const { add } = useCart();
  const [view, setView] = useState(0);
  const [qty, setQty] = useState(1);

  const { data, loading, error } = useResource((signal) => api.product(id, signal), [id]);
  const product = data?.product;
  const related = data?.related || [];

  if (error) {
    return (
      <Empty
        icon={PackageSearch}
        title={error.status === 404 ? "That product is gone" : "Could not load this product"}
        body={
          error.status === 404
            ? "It may have been unpublished since you last saw it. The rest of the catalog is still here."
            : error.message
        }
        action="Back to the catalog"
        actionTo="/shop"
      />
    );
  }

  if (!product) {
    return (
      <div className="mx-auto max-w-[1400px] px-5 py-12">
        <DetailSkeleton />
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-[1400px] px-5 pb-24">
      <nav className="flex flex-wrap items-center gap-1.5 py-6 text-[0.75rem] text-stone">
        <Link to="/shop" className="link-rule">
          Shop
        </Link>
        <span className="text-stone-light">/</span>
        <Link to={`/shop?category=${encodeURIComponent(product.category)}`} className="link-rule">
          {product.category}
        </Link>
        {product.subcategory && (
          <>
            <span className="text-stone-light">/</span>
            <span className="text-stone-light">{product.subcategory}</span>
          </>
        )}
      </nav>

      <div className="grid gap-12 lg:grid-cols-2 lg:gap-16">
        {/* ---------------- Gallery ---------------- */}
        <div className="lg:sticky lg:top-28 lg:self-start">
          <ProductVisual
            product={product}
            offset={view}
            className="aspect-4/5 w-full rounded-card"
            glyphClass="w-[22%] h-[22%]"
          />
          <div className="mt-3 grid grid-cols-4 gap-3">
            {VARIANTS.map((variant, index) => (
              <button
                key={variant}
                type="button"
                onClick={() => setView(index)}
                aria-label={`View ${index + 1} of ${VARIANTS.length}`}
                aria-pressed={index === view}
                className={`rounded-card transition-opacity ${
                  index === view ? "ring-1 ring-ink" : "opacity-60 hover:opacity-100"
                }`}
              >
                <ProductVisual
                  product={product}
                  offset={index}
                  chrome={false}
                  className="aspect-square w-full rounded-card"
                  glyphClass="w-[30%] h-[30%]"
                />
              </button>
            ))}
          </div>
          <p className="mt-3 text-[0.6875rem] leading-relaxed text-stone-light">
            Panels are generated from the product's category and id — this demo catalog ships
            without photography.
          </p>
        </div>

        {/* ---------------- Detail ---------------- */}
        <div className="lg:pt-2">
          <p className="eyebrow">{product.brand}</p>
          <h1 className="font-display mt-3 text-3xl leading-tight sm:text-[2.5rem]">
            {product.title}
          </h1>

          <div className="mt-4 flex items-center gap-4">
            <Stars value={product.rating} size={14} />
            {product.subcategory && (
              <span className="text-[0.8125rem] text-stone-light">{product.subcategory}</span>
            )}
          </div>

          <p className="font-display mt-6 text-[1.75rem] tabular-nums">{money(product.price)}</p>

          <p className="mt-6 max-w-prose text-[0.9375rem] leading-relaxed text-ink-soft">
            {product.description || product.blurb}
          </p>

          <div className="mt-9 flex flex-wrap items-stretch gap-3">
            <div className="flex items-center border border-rule-dark">
              <button
                type="button"
                onClick={() => setQty((current) => Math.max(1, current - 1))}
                disabled={qty <= 1}
                className="flex h-11 w-11 items-center justify-center text-stone transition-colors disabled:opacity-30 enabled:hover:text-ink"
                aria-label="Decrease quantity"
              >
                <Minus size={14} strokeWidth={1.75} />
              </button>
              <span className="w-8 text-center text-sm tabular-nums" aria-live="polite">
                {qty}
              </span>
              <button
                type="button"
                onClick={() => setQty((current) => Math.min(10, current + 1))}
                disabled={qty >= 10}
                className="flex h-11 w-11 items-center justify-center text-stone transition-colors disabled:opacity-30 enabled:hover:text-ink"
                aria-label="Increase quantity"
              >
                <Plus size={14} strokeWidth={1.75} />
              </button>
            </div>
            <button
              type="button"
              onClick={() => add(product, qty)}
              className="btn btn-ink h-11 flex-1 justify-center px-8 sm:flex-none"
            >
              Add to bag
            </button>
          </div>

          <ul className="mt-8 space-y-2.5">
            {NOTES.map(({ Icon, text }) => (
              <li key={text} className="flex items-center gap-2.5 text-[0.8125rem] text-stone">
                <Icon size={14} strokeWidth={1.5} className="shrink-0 text-stone-light" />
                {text}
              </li>
            ))}
          </ul>

          <dl className="mt-10 text-[0.8125rem]">
            <Spec label="Maker">{product.brand}</Spec>
            <Spec label="Category">
              {product.category}
              {product.subcategory ? ` · ${product.subcategory}` : ""}
            </Spec>
            <Spec label="Colour">{product.color}</Spec>
            <Spec label="Size">{product.size}</Spec>
            <Spec label="Rating">{product.rating?.toFixed(1)} out of 5</Spec>
          </dl>

          {product.tags?.length > 0 && (
            <div className="mt-8">
              <p className="eyebrow">Also filed under</p>
              <div className="mt-3 flex flex-wrap gap-2">
                {product.tags.map((tag) => (
                  <Link
                    key={tag}
                    to={`/shop?q=${encodeURIComponent(tag)}`}
                    className="pill hover:border-ink hover:text-ink"
                  >
                    {tag}
                  </Link>
                ))}
              </div>
            </div>
          )}
        </div>
      </div>

      {/* ---------------- Related ---------------- */}
      <section className="mt-24">
        <div className="flex flex-wrap items-end justify-between gap-4 border-b border-rule pb-5">
          <div>
            <p className="eyebrow">Same shelf</p>
            <h2 className="font-display mt-2 text-2xl">Goes with this</h2>
          </div>
          <Link
            to={`/shop?category=${encodeURIComponent(product.category)}`}
            className="link-rule pb-1 text-[0.8125rem]"
          >
            All {product.category}
          </Link>
        </div>
        <div className="mt-8">
          {loading && !related.length ? (
            <StripSkeleton />
          ) : (
            <div className="grid grid-cols-2 gap-x-5 gap-y-10 lg:grid-cols-4">
              {related.map((item) => (
                <ProductCard key={item.id} product={item} />
              ))}
            </div>
          )}
        </div>
      </section>
    </div>
  );
}
