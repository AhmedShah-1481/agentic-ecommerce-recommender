import { Link } from "react-router-dom";
import { CATEGORY_ORDER, categoryVisual } from "../lib/productVisual";

/**
 * The frame both auth pages sit in.
 *
 * A split rather than a centred card: the form gets a quiet column and the panel
 * side carries the same generated-gradient language as the catalog, so signing in
 * does not look like it happens in a different application.
 */
export default function AuthShell({ eyebrow, title, lede, children, footer }) {
  return (
    <div className="mx-auto grid max-w-[1400px] items-stretch gap-14 px-5 py-16 lg:grid-cols-2 lg:gap-20 lg:py-24">
      <div className="mx-auto w-full max-w-sm lg:mx-0 lg:pt-8">
        <p className="eyebrow">{eyebrow}</p>
        <h1 className="font-display mt-3 text-3xl sm:text-[2.5rem]">{title}</h1>
        <p className="mt-4 text-[0.9375rem] leading-relaxed text-stone">{lede}</p>

        <div className="mt-9">{children}</div>

        <p className="mt-8 border-t border-rule pt-6 text-[0.8125rem] text-stone">{footer}</p>
      </div>

      {/* Decorative only, and hidden on small screens rather than stacked - below
          the form it would be a screenful of gradient between the button and the
          footer. */}
      <div className="hidden lg:block">
        <div className="grid h-full grid-cols-2 gap-3">
          {CATEGORY_ORDER.slice(0, 4).map((category, index) => {
            const { gradient, ink, Icon } = categoryVisual(category, index);
            return (
              <div
                key={category}
                className="relative flex min-h-[9rem] items-center justify-center rounded-card"
                style={{ background: gradient }}
              >
                <Icon size={30} strokeWidth={1} style={{ color: ink, opacity: 0.5 }} />
                <span
                  className="absolute bottom-3 left-3 text-[0.625rem] uppercase tracking-[0.18em]"
                  style={{ color: ink, opacity: 0.6 }}
                >
                  {category}
                </span>
              </div>
            );
          })}
        </div>
        <p className="mt-4 text-[0.75rem] leading-relaxed text-stone">
          <Link to="/shop" className="link-rule">
            Browsing works signed out
          </Link>{" "}
          — an account is only needed for recommendations, because they are built from one
          person's own behaviour.
        </p>
      </div>
    </div>
  );
}
