import { Link } from "react-router-dom";
import { Minus, Plus, ShoppingBag, Trash2, X } from "lucide-react";
import ProductVisual from "./ProductVisual";
import { money, plural } from "../lib/format";
import { useCart } from "../state/CartContext";
import { useBodyLock, useEscape } from "../lib/useBodyLock";

/**
 * Slide-over cart.
 *
 * Opens by itself on add-to-cart, which is the only confirmation that action
 * gets - a toast would say less and take the same amount of screen.
 *
 * Checkout is out of scope, so the button says so plainly rather than leading to
 * a dead route. Everything above it - quantities, the running subtotal, the
 * empty state - is real.
 */
export default function CartDrawer() {
  const { lines, count, subtotal, setQty, remove, open, setOpen } = useCart();
  const close = () => setOpen(false);

  useBodyLock(open);
  useEscape(open, close);

  return (
    <div className={`fixed inset-0 z-50 ${open ? "" : "pointer-events-none"}`} aria-hidden={!open}>
      <div
        onClick={close}
        className={`absolute inset-0 bg-ink/40 transition-opacity duration-300 ${
          open ? "opacity-100" : "opacity-0"
        }`}
      />

      <aside
        className={`absolute inset-y-0 right-0 flex w-full max-w-md flex-col bg-bone shadow-lift transition-transform duration-300 ease-out ${
          open ? "translate-x-0" : "translate-x-full"
        }`}
        aria-label="Shopping cart"
      >
        <header className="flex items-center justify-between border-b border-rule px-6 py-5">
          <h2 className="font-display text-lg">
            Your bag{count > 0 && <span className="text-stone"> ({count})</span>}
          </h2>
          <button type="button" onClick={close} aria-label="Close cart" className="p-1">
            <X size={20} strokeWidth={1.5} />
          </button>
        </header>

        {lines.length === 0 ? (
          <div className="flex flex-1 flex-col items-center justify-center px-8 text-center">
            <div className="flex h-16 w-16 items-center justify-center rounded-pill border border-rule-dark">
              <ShoppingBag size={22} strokeWidth={1.25} className="text-stone" />
            </div>
            <p className="font-display mt-6 text-xl">Nothing in here yet</p>
            <p className="mt-2 max-w-xs text-sm leading-relaxed text-stone">
              Anything you add is kept on this device. It also tells the recommender what you
              are actually interested in.
            </p>
            <Link to="/shop" onClick={close} className="btn btn-ink mt-7">
              Start browsing
            </Link>
          </div>
        ) : (
          <>
            <ul className="flex-1 overflow-y-auto px-6">
              {lines.map(({ product, qty }) => (
                <li key={product.id} className="flex gap-4 border-b border-rule py-5">
                  <Link to={`/product/${product.id}`} onClick={close} className="shrink-0">
                    <ProductVisual
                      product={product}
                      chrome={false}
                      glyphClass="w-[34%] h-[34%]"
                      className="h-24 w-20 rounded-card"
                    />
                  </Link>

                  <div className="flex min-w-0 flex-1 flex-col">
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0">
                        {product.brand && <p className="eyebrow">{product.brand}</p>}
                        <Link
                          to={`/product/${product.id}`}
                          onClick={close}
                          className="font-display mt-1 block truncate text-sm hover:text-terracotta"
                        >
                          {product.title}
                        </Link>
                      </div>
                      <span className="text-sm tabular-nums">{money(product.price * qty)}</span>
                    </div>

                    <div className="mt-auto flex items-center justify-between pt-3">
                      <div className="flex items-center border border-rule-dark">
                        <button
                          type="button"
                          onClick={() => setQty(product.id, qty - 1)}
                          aria-label={`Reduce quantity of ${product.title}`}
                          className="px-2.5 py-1.5 text-stone hover:text-ink"
                        >
                          <Minus size={13} strokeWidth={1.75} />
                        </button>
                        <span className="min-w-8 text-center text-[0.8125rem] tabular-nums">
                          {qty}
                        </span>
                        <button
                          type="button"
                          onClick={() => setQty(product.id, qty + 1)}
                          aria-label={`Increase quantity of ${product.title}`}
                          className="px-2.5 py-1.5 text-stone hover:text-ink"
                        >
                          <Plus size={13} strokeWidth={1.75} />
                        </button>
                      </div>
                      <button
                        type="button"
                        onClick={() => remove(product.id)}
                        className="p-1 text-stone-light hover:text-terracotta"
                        aria-label={`Remove ${product.title}`}
                      >
                        <Trash2 size={15} strokeWidth={1.5} />
                      </button>
                    </div>
                  </div>
                </li>
              ))}
            </ul>

            <footer className="border-t border-rule px-6 py-6">
              <div className="flex items-baseline justify-between">
                <span className="text-sm">
                  Subtotal
                  <span className="text-stone"> · {plural(count, "item")}</span>
                </span>
                <span className="font-display text-xl tabular-nums">{money(subtotal)}</span>
              </div>
              <p className="mt-1.5 text-[0.6875rem] text-stone-light">
                Shipping and taxes calculated at checkout.
              </p>
              <button type="button" disabled className="btn btn-ink mt-5 w-full">
                Checkout unavailable in this demo
              </button>
              <Link to="/cart" onClick={close} className="btn btn-quiet mt-2.5 w-full">
                View full bag
              </Link>
            </footer>
          </>
        )}
      </aside>
    </div>
  );
}
