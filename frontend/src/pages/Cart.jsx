import { Link } from "react-router-dom";
import { Minus, Plus, ShoppingBag, Sparkles, X } from "lucide-react";
import { useCart } from "../state/CartContext";
import { money, plural } from "../lib/format";
import ProductVisual from "../components/ProductVisual";
import Empty from "../components/Empty";

/**
 * The bag as a page, alongside the drawer.
 *
 * Checkout is out of scope, and the button says so rather than leading anyone
 * into a flow that does not exist. Everything else behaves like a real bag,
 * because the add-to-bag event is the highest-weighted signal the recommender
 * has - a cart that felt fake would not get used, and the pipeline would have
 * nothing to read.
 */
export default function Cart() {
  const { lines, count, subtotal, setQty, remove, clear } = useCart();

  if (lines.length === 0) {
    return (
      <Empty
        icon={ShoppingBag}
        title="Your bag is empty"
        body="Nothing in here yet. The bag is stored in this browser only — there is no checkout in this demo, but what you add still teaches the recommender what you are interested in."
        action="Browse the catalog"
        actionTo="/shop"
      >
        <Link to="/for-you" className="link-rule mt-4 text-[0.8125rem] text-stone">
          or see what has been recommended for you
        </Link>
      </Empty>
    );
  }

  return (
    <div className="mx-auto max-w-[1400px] px-5 pb-24">
      <header className="flex flex-wrap items-end justify-between gap-4 border-b border-rule py-10">
        <div>
          <p className="eyebrow">Bag</p>
          <h1 className="font-display mt-3 text-3xl sm:text-[2.5rem]">
            {plural(count, "piece")} set aside
          </h1>
        </div>
        <button type="button" onClick={clear} className="link-rule pb-1 text-[0.8125rem] text-stone">
          Empty the bag
        </button>
      </header>

      <div className="grid gap-12 pt-10 lg:grid-cols-12 lg:gap-16">
        <ul className="lg:col-span-8">
          {lines.map(({ product, qty }) => (
            <li
              key={product.id}
              className="flex gap-5 border-b border-rule py-6 first:pt-0 sm:gap-7"
            >
              <Link to={`/product/${product.id}`} className="shrink-0">
                <ProductVisual
                  product={product}
                  chrome={false}
                  className="aspect-square w-24 rounded-card sm:w-28"
                  glyphClass="w-[34%] h-[34%]"
                />
              </Link>

              <div className="flex min-w-0 flex-1 flex-col">
                <div className="flex items-start justify-between gap-4">
                  <div className="min-w-0">
                    <p className="eyebrow">{product.brand}</p>
                    <h2 className="font-display mt-1.5 text-lg leading-snug">
                      <Link to={`/product/${product.id}`} className="link-rule">
                        {product.title}
                      </Link>
                    </h2>
                    <p className="mt-1 text-[0.75rem] text-stone-light">
                      {[product.color, product.size].filter(Boolean).join(" · ")}
                    </p>
                  </div>
                  <button
                    type="button"
                    onClick={() => remove(product.id)}
                    aria-label={`Remove ${product.title}`}
                    className="p-1 text-stone-light transition-colors hover:text-terracotta"
                  >
                    <X size={16} strokeWidth={1.5} />
                  </button>
                </div>

                <div className="mt-auto flex flex-wrap items-center justify-between gap-4 pt-5">
                  <div className="flex items-center border border-rule-dark">
                    <button
                      type="button"
                      onClick={() => setQty(product.id, qty - 1)}
                      className="flex h-9 w-9 items-center justify-center text-stone hover:text-ink"
                      aria-label="Decrease quantity"
                    >
                      <Minus size={13} strokeWidth={1.75} />
                    </button>
                    <span className="w-7 text-center text-[0.8125rem] tabular-nums">{qty}</span>
                    <button
                      type="button"
                      onClick={() => setQty(product.id, qty + 1)}
                      className="flex h-9 w-9 items-center justify-center text-stone hover:text-ink"
                      aria-label="Increase quantity"
                    >
                      <Plus size={13} strokeWidth={1.75} />
                    </button>
                  </div>

                  <p className="text-sm tabular-nums">
                    {qty > 1 && (
                      <span className="text-stone-light">{money(product.price)} each · </span>
                    )}
                    <span className="font-medium">{money(product.price * qty)}</span>
                  </p>
                </div>
              </div>
            </li>
          ))}
        </ul>

        <aside className="lg:col-span-4">
          <div className="sticky top-28 border border-rule bg-paper p-7">
            <h2 className="font-display text-xl">Summary</h2>

            <dl className="mt-6 space-y-3 text-sm">
              <div className="flex justify-between">
                <dt className="text-stone">{plural(count, "item")}</dt>
                <dd className="tabular-nums">{money(subtotal)}</dd>
              </div>
              <div className="flex justify-between">
                <dt className="text-stone">Shipping</dt>
                <dd className={subtotal >= 150 ? "text-terracotta" : "text-stone-light"}>
                  {subtotal >= 150 ? "Complimentary" : "Calculated at checkout"}
                </dd>
              </div>
              <div className="flex justify-between border-t border-rule pt-3">
                <dt className="font-display text-lg">Subtotal</dt>
                <dd className="font-display text-lg tabular-nums">{money(subtotal)}</dd>
              </div>
            </dl>

            {subtotal < 150 && (
              <p className="mt-4 bg-clay/60 p-3 text-[0.75rem] leading-relaxed">
                {money(150 - subtotal)} more for complimentary shipping.
              </p>
            )}

            <button type="button" disabled className="btn btn-ink mt-6 w-full justify-center opacity-40">
              Checkout unavailable in this demo
            </button>
            <p className="mt-3 text-[0.6875rem] leading-relaxed text-stone">
              There is no payment step and no cart table — the bag lives in this browser. Adding to
              it is recorded as behaviour, which is the part that matters here.
            </p>

            <Link to="/for-you" className="btn btn-outline mt-6 w-full justify-center">
              <Sparkles size={14} strokeWidth={1.5} />
              See your edit
            </Link>
          </div>
        </aside>
      </div>
    </div>
  );
}
