import { useState } from "react";
import { Link } from "react-router-dom";
import { ArrowRight, Check } from "lucide-react";
import { CATEGORY_ORDER } from "../lib/productVisual";

/**
 * Footer: newsletter, link columns, legal.
 *
 * The newsletter is deliberately local-only. There is no subscriber table and
 * no mailing integration, so the form confirms and stops rather than pretending
 * to have stored an address - a "thanks, you're on the list" that quietly
 * discards the email is worse than saying nothing.
 */

const COLUMNS = [
  {
    title: "Shop",
    links: [
      { label: "All products", to: "/shop" },
      { label: "New arrivals", to: "/shop?sort=newest" },
      { label: "Top rated", to: "/shop?sort=rating" },
      { label: "Recommended for you", to: "/for-you" },
    ],
  },
  {
    title: "Help",
    links: [
      { label: "Shipping & returns", to: "/shop" },
      { label: "Size guide", to: "/shop" },
      { label: "Care instructions", to: "/shop" },
      { label: "Contact", to: "/shop" },
    ],
  },
];

export default function Footer() {
  const [email, setEmail] = useState("");
  const [done, setDone] = useState(false);

  return (
    <footer className="mt-24 border-t border-rule bg-bone">
      <div className="mx-auto max-w-[1400px] px-5">
        {/* ---- Newsletter ---- */}
        <div className="grid gap-10 border-b border-rule py-16 lg:grid-cols-2 lg:gap-20">
          <div>
            <p className="eyebrow">The list</p>
            <h2 className="font-display mt-3 text-3xl leading-tight sm:text-[2.5rem]">
              Considered goods,
              <br />
              <span className="italic">once a fortnight.</span>
            </h2>
          </div>
          <div className="lg:pt-10">
            <p className="max-w-md text-sm leading-relaxed text-stone">
              New arrivals, restocks, and the occasional note on how something was made. No
              more than twice a month.
            </p>
            {done ? (
              <p className="mt-6 flex items-center gap-2 text-sm text-terracotta">
                <Check size={16} strokeWidth={1.75} />
                Noted — this demo keeps it on your device only.
              </p>
            ) : (
              <form
                onSubmit={(event) => {
                  event.preventDefault();
                  setDone(true);
                }}
                className="mt-6 flex max-w-md gap-2"
              >
                <label htmlFor="newsletter" className="sr-only">
                  Email address
                </label>
                <input
                  id="newsletter"
                  type="email"
                  required
                  value={email}
                  onChange={(event) => setEmail(event.target.value)}
                  placeholder="you@example.com"
                  className="field"
                />
                <button type="submit" className="btn btn-ink shrink-0">
                  Join
                  <ArrowRight size={14} strokeWidth={1.75} />
                </button>
              </form>
            )}
          </div>
        </div>

        {/* ---- Link columns ---- */}
        <div className="grid gap-10 py-14 sm:grid-cols-2 lg:grid-cols-4">
          <div>
            <Link to="/" className="font-display text-xl">
              Bazario<span className="text-terracotta">.</span>
            </Link>
            <p className="mt-4 max-w-xs text-sm leading-relaxed text-stone">
              A small catalog of everyday things, chosen for how they wear rather than how they
              photograph.
            </p>
          </div>

          <div>
            <p className="eyebrow">Categories</p>
            <ul className="mt-4 space-y-2.5">
              {CATEGORY_ORDER.map((category) => (
                <li key={category}>
                  <Link
                    to={`/shop?category=${encodeURIComponent(category)}`}
                    className="link-rule text-sm text-stone"
                  >
                    {category}
                  </Link>
                </li>
              ))}
            </ul>
          </div>

          {COLUMNS.map((column) => (
            <div key={column.title}>
              <p className="eyebrow">{column.title}</p>
              <ul className="mt-4 space-y-2.5">
                {column.links.map((link) => (
                  <li key={link.label}>
                    <Link to={link.to} className="link-rule text-sm text-stone">
                      {link.label}
                    </Link>
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </div>

        {/* ---- Legal ---- */}
        <div className="flex flex-col gap-3 border-t border-rule py-7 text-[0.6875rem] tracking-wide text-stone-light sm:flex-row sm:items-center sm:justify-between">
          <p>© {new Date().getFullYear()} Bazario Mart. A demonstration storefront.</p>
          <p className="flex gap-5">
            <span>Privacy</span>
            <span>Terms</span>
            <span>Accessibility</span>
          </p>
        </div>
      </div>
    </footer>
  );
}
