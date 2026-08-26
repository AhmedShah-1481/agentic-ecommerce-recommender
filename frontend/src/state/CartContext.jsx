import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { trackClick } from "../lib/tracker";

/**
 * The cart.
 *
 * Client-side only - there is no cart table and checkout is out of scope - but
 * it is the strongest behavioural signal the recommender has, so adding to it
 * emits an `add_to_cart` event. That event is weighted highest in the profile
 * and it is one of the two things that sets `high_signal`, which is what
 * unblocks a regeneration.
 *
 * localStorage rather than sessionStorage: a cart that empties itself when the
 * tab closes reads as a bug. Line items store a snapshot of the product rather
 * than an id, so the cart and the drawer render without a fetch per line.
 */
const STORAGE_KEY = "ll_cart";
const CartContext = createContext(null);

function read() {
  try {
    const raw = JSON.parse(localStorage.getItem(STORAGE_KEY) || "[]");
    return Array.isArray(raw) ? raw.filter((line) => line && line.product && line.qty > 0) : [];
  } catch {
    return [];
  }
}

export function CartProvider({ children }) {
  const [lines, setLines] = useState(read);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(lines));
    } catch {
      /* private mode or a full quota - the cart still works for this session */
    }
  }, [lines]);

  const add = useCallback((product, qty = 1) => {
    setLines((previous) => {
      const existing = previous.find((line) => line.product.id === product.id);
      if (existing) {
        return previous.map((line) =>
          line.product.id === product.id ? { ...line, qty: line.qty + qty } : line
        );
      }
      return [...previous, { product, qty }];
    });

    // The event, not the badge, is the point. Fired here rather than at each
    // call site so no button can add to the cart without being recorded.
    trackClick("add_to_cart", { productId: product.id, label: product.title });
    setOpen(true);
  }, []);

  const setQty = useCallback((productId, qty) => {
    setLines((previous) =>
      qty <= 0
        ? previous.filter((line) => line.product.id !== productId)
        : previous.map((line) => (line.product.id === productId ? { ...line, qty } : line))
    );
  }, []);

  const remove = useCallback(
    (productId) => setLines((previous) => previous.filter((line) => line.product.id !== productId)),
    []
  );

  const clear = useCallback(() => setLines([]), []);

  const value = useMemo(() => {
    const count = lines.reduce((total, line) => total + line.qty, 0);
    const subtotal = lines.reduce((total, line) => total + line.qty * (line.product.price || 0), 0);
    return { lines, count, subtotal, add, setQty, remove, clear, open, setOpen };
  }, [lines, add, setQty, remove, clear, open]);

  return <CartContext.Provider value={value}>{children}</CartContext.Provider>;
}

export function useCart() {
  const context = useContext(CartContext);
  if (!context) throw new Error("useCart must be used inside CartProvider");
  return context;
}
