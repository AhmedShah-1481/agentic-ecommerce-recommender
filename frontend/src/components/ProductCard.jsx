import { Link } from "react-router-dom";
import { Plus } from "lucide-react";
import ProductVisual from "./ProductVisual";
import Stars from "./Stars";
import { money } from "../lib/format";
import { useCart } from "../state/CartContext";
import { trackClick } from "../lib/tracker";

/**
 * One product in a grid, a strip, or a recommendation slate.
 *
 * The same component in all three so a recommended product is visibly the same
 * object as a browsed one - the agent's output should look like the store, not
 * like a widget bolted onto it. `reason` and `rank` are what a recommendation
 * adds, and the reason renders as a bordered annotation under the card rather
 * than inside a tooltip: it is the output of the whole system, and hiding it
 * behind a hover would bury the only visible evidence that any of this
 * reasoning happened.
 *
 * The link is an absolutely positioned overlay rather than a wrapper, so the
 * quick-add button can sit above it without nesting a button inside an anchor.
 */
export default function ProductCard({ product, reason = "", rank = null, className = "" }) {
  const { add } = useCart();

  return (
    <article className={`group relative flex flex-col ${className}`}>
      <div className="relative overflow-hidden rounded-card bg-rule">
        <ProductVisual
          product={product}
          className="aspect-4/5 w-full transition-transform duration-500 ease-out group-hover:scale-[1.03]"
        />

        {rank !== null && (
          <span className="font-display absolute top-0 left-0 flex h-9 w-9 items-center justify-center bg-ink text-sm text-bone">
            {rank}
          </span>
        )}

        {/* Quick add. Hover-revealed on pointer devices, always visible where
            there is no hover to reveal it with. */}
        <button
          type="button"
          onClick={() => add(product)}
          className="btn btn-ink absolute inset-x-0 bottom-0 z-20 translate-y-full py-3 opacity-0 transition duration-200 group-hover:translate-y-0 group-hover:opacity-100 focus-visible:translate-y-0 focus-visible:opacity-100 max-md:translate-y-0 max-md:opacity-100"
        >
          <Plus size={13} strokeWidth={2} />
          Add to cart
        </button>
      </div>

      <div className="mt-4 flex flex-1 flex-col">
        {product.brand && <p className="eyebrow">{product.brand}</p>}

        <h3 className="font-display mt-1.5 text-[0.9375rem] leading-snug">
          <Link
            to={`/product/${product.id}`}
            // `product_click` is the event a card can record that a page view
            // cannot: it says which of twelve visible products was chosen.
            onClick={() => trackClick("product_click", { productId: product.id, label: product.title })}
            className="absolute inset-0 z-10"
          >
            <span className="sr-only">{product.title}</span>
          </Link>
          <span className="transition-colors group-hover:text-terracotta">{product.title}</span>
        </h3>

        <div className="mt-2 flex items-center justify-between gap-3">
          <span className="text-sm tabular-nums">{money(product.price)}</span>
          <Stars value={product.rating} />
        </div>

        {reason && (
          <p className="mt-3 border-l-2 border-terracotta bg-clay/60 py-2 pr-2 pl-3 text-[0.8125rem] leading-relaxed text-ink/85">
            {reason}
          </p>
        )}
      </div>
    </article>
  );
}
