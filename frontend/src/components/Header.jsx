import { useEffect, useRef, useState } from "react";
import { Link, NavLink, useLocation, useNavigate } from "react-router-dom";
import { Menu, Search, ShoppingBag, Sparkles, User, X } from "lucide-react";
import { useAuth } from "../state/AuthContext";
import { useCart } from "../state/CartContext";
import { trackSearch } from "../lib/tracker";
import { CATEGORY_ORDER } from "../lib/productVisual";
import { useBodyLock, useEscape } from "../lib/useBodyLock";

/**
 * Sticky header: wordmark, nav, search, auth state, cart.
 *
 * The search field tracks and navigates separately. Typing fires the debounced
 * `search` event - 600ms, so a phrase is one event rather than one per keystroke
 * - while submitting is what actually navigates. That split matters: the
 * behavioural signal is the words someone considered, and half of those are
 * never submitted.
 */

const NAV = [
  { to: "/shop", label: "Shop all" },
  { to: "/for-you", label: "For you" },
];

function SearchField({ id, autoFocus = false, onSubmitted, className = "" }) {
  const navigate = useNavigate();
  const location = useLocation();
  const [value, setValue] = useState("");

  // Keep the box in step with the URL: arriving on /shop?q=merino, or clearing
  // the term from the sidebar, should both be reflected here.
  useEffect(() => {
    const params = new URLSearchParams(location.search);
    setValue(location.pathname === "/shop" ? params.get("q") || "" : "");
  }, [location.pathname, location.search]);

  const submit = (event) => {
    event.preventDefault();
    const term = value.trim();
    navigate(term ? `/shop?q=${encodeURIComponent(term)}` : "/shop");
    onSubmitted?.();
  };

  return (
    <form onSubmit={submit} className={`relative ${className}`} role="search">
      <label htmlFor={id} className="sr-only">
        Search products
      </label>
      <Search
        size={15}
        strokeWidth={1.5}
        className="pointer-events-none absolute top-1/2 left-3 -translate-y-1/2 text-stone-light"
      />
      <input
        id={id}
        type="search"
        value={value}
        autoFocus={autoFocus}
        placeholder="Search the catalog"
        onChange={(event) => {
          setValue(event.target.value);
          trackSearch(event.target.value);
        }}
        className="field bg-transparent pl-9 text-[0.8125rem]"
      />
    </form>
  );
}

function MobileDrawer({ open, onClose }) {
  const { user, logout } = useAuth();
  useBodyLock(open);
  useEscape(open, onClose);

  return (
    <div
      className={`fixed inset-0 z-50 lg:hidden ${open ? "" : "pointer-events-none"}`}
      aria-hidden={!open}
    >
      <div
        onClick={onClose}
        className={`absolute inset-0 bg-ink/40 transition-opacity duration-300 ${
          open ? "opacity-100" : "opacity-0"
        }`}
      />
      <div
        className={`absolute inset-y-0 left-0 flex w-[86%] max-w-sm flex-col bg-bone transition-transform duration-300 ease-out ${
          open ? "translate-x-0" : "-translate-x-full"
        }`}
      >
        <div className="flex items-center justify-between border-b border-rule px-5 py-4">
          <span className="font-display text-lg">
            Bazario<span className="text-terracotta">.</span>
          </span>
          <button type="button" onClick={onClose} aria-label="Close menu" className="p-1">
            <X size={20} strokeWidth={1.5} />
          </button>
        </div>

        <div className="flex-1 overflow-y-auto px-5 py-6">
          <SearchField id="search-mobile" onSubmitted={onClose} />

          <nav className="mt-8 flex flex-col">
            {NAV.map((item) => (
              <Link
                key={item.to}
                to={item.to}
                onClick={onClose}
                className="font-display border-b border-rule py-3.5 text-xl"
              >
                {item.label}
              </Link>
            ))}
          </nav>

          <p className="eyebrow mt-9">Categories</p>
          <nav className="mt-3 flex flex-col">
            {CATEGORY_ORDER.map((category) => (
              <Link
                key={category}
                to={`/shop?category=${encodeURIComponent(category)}`}
                onClick={onClose}
                className="py-2.5 text-sm text-stone"
              >
                {category}
              </Link>
            ))}
          </nav>
        </div>

        <div className="border-t border-rule px-5 py-5">
          {user ? (
            <div className="flex items-center justify-between gap-3">
              <span className="truncate text-sm">{user.name || user.email}</span>
              <button
                type="button"
                onClick={() => {
                  logout();
                  onClose();
                }}
                className="link-rule text-[0.8125rem] text-stone"
              >
                Sign out
              </button>
            </div>
          ) : (
            <div className="flex gap-3">
              <Link to="/login" onClick={onClose} className="btn btn-outline flex-1">
                Sign in
              </Link>
              <Link to="/register" onClick={onClose} className="btn btn-primary flex-1">
                Join
              </Link>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

export default function Header() {
  const { user, ready, logout } = useAuth();
  const { count, setOpen: setCartOpen } = useCart();
  const [menuOpen, setMenuOpen] = useState(false);
  const [accountOpen, setAccountOpen] = useState(false);
  const location = useLocation();
  const accountRef = useRef(null);

  // A client-side navigation leaves overlays mounted, so they have to be closed
  // explicitly - otherwise tapping a link inside the drawer changes the page
  // behind a drawer that is still covering it.
  useEffect(() => {
    setMenuOpen(false);
    setAccountOpen(false);
  }, [location.pathname, location.search]);

  useEffect(() => {
    if (!accountOpen) return undefined;
    const onPointerDown = (event) => {
      if (!accountRef.current?.contains(event.target)) setAccountOpen(false);
    };
    document.addEventListener("pointerdown", onPointerDown);
    return () => document.removeEventListener("pointerdown", onPointerDown);
  }, [accountOpen]);

  const navClass = ({ isActive }) =>
    `link-rule text-[0.8125rem] tracking-wide ${isActive ? "text-terracotta" : ""}`;

  return (
    <>
      {/* Promo bar. Two claims, and the right-hand one is the honest pitch for
          what this build actually is. */}
      <div className="bg-ink text-bone">
        <div className="mx-auto flex max-w-[1400px] items-center justify-center gap-3 px-5 py-2.5 text-[0.6875rem] tracking-[0.14em] uppercase sm:justify-between">
          <span>Complimentary shipping over $150</span>
          <Link to="/for-you" className="link-rule hidden items-center gap-1.5 sm:inline-flex">
            <Sparkles size={12} strokeWidth={1.5} className="text-terracotta" />
            Recommendations that learn as you browse
          </Link>
        </div>
      </div>

      <header className="sticky top-0 z-40 border-b border-rule bg-bone/85 backdrop-blur-md">
        <div className="mx-auto flex max-w-[1400px] items-center gap-4 px-5 py-4 lg:gap-10">
          <button
            type="button"
            onClick={() => setMenuOpen(true)}
            aria-label="Open menu"
            className="-ml-1 p-1 lg:hidden"
          >
            <Menu size={20} strokeWidth={1.5} />
          </button>

          <Link to="/" className="font-display shrink-0 text-xl leading-none tracking-tight">
            Bazario<span className="text-terracotta">.</span>
          </Link>

          <nav className="hidden items-center gap-7 lg:flex">
            {NAV.map((item) => (
              <NavLink key={item.to} to={item.to} className={navClass}>
                {item.label}
              </NavLink>
            ))}
          </nav>

          <SearchField id="search-desktop" className="ml-auto hidden w-64 lg:block" />

          <div className="ml-auto flex items-center gap-1 lg:ml-0">
            {/* Auth state waits for /api/auth/me rather than guessing. Rendering
                "Sign in" during the round trip would flash the wrong state at
                every signed-in visitor on every cold load. */}
            {!ready ? (
              <div className="skeleton mr-1 h-4 w-16 rounded-pill" />
            ) : user ? (
              <div ref={accountRef} className="relative hidden lg:block">
                <button
                  type="button"
                  onClick={() => setAccountOpen((value) => !value)}
                  className="flex items-center gap-2 px-2 py-1.5 text-[0.8125rem]"
                  aria-expanded={accountOpen}
                >
                  <User size={16} strokeWidth={1.5} />
                  <span className="max-w-[9rem] truncate">{user.name || user.email}</span>
                </button>
                {accountOpen && (
                  <div className="absolute right-0 mt-2 w-52 rounded-card border border-rule bg-paper py-1.5 shadow-lift">
                    <Link to="/for-you" className="block px-4 py-2 text-[0.8125rem] hover:bg-bone">
                      Your recommendations
                    </Link>
                    {user.is_admin && (
                      <a
                        href={`${import.meta.env.VITE_API_URL || "http://localhost:8000"}/admin`}
                        className="block px-4 py-2 text-[0.8125rem] hover:bg-bone"
                      >
                        Admin panel
                      </a>
                    )}
                    <button
                      type="button"
                      onClick={logout}
                      className="block w-full px-4 py-2 text-left text-[0.8125rem] text-stone hover:bg-bone"
                    >
                      Sign out
                    </button>
                  </div>
                )}
              </div>
            ) : (
              <Link to="/login" className="hidden px-2 py-1.5 text-[0.8125rem] lg:block">
                <span className="link-rule">Sign in</span>
              </Link>
            )}

            <button
              type="button"
              onClick={() => setCartOpen(true)}
              className="relative p-2"
              aria-label={`Cart, ${count} ${count === 1 ? "item" : "items"}`}
            >
              <ShoppingBag size={19} strokeWidth={1.5} />
              {count > 0 && (
                <span className="absolute top-0.5 right-0.5 flex h-4 min-w-4 items-center justify-center rounded-pill bg-terracotta px-1 text-[0.625rem] font-medium text-bone tabular-nums">
                  {count}
                </span>
              )}
            </button>
          </div>
        </div>
      </header>

      <MobileDrawer open={menuOpen} onClose={() => setMenuOpen(false)} />
    </>
  );
}
