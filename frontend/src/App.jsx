import { useEffect } from "react";
import { BrowserRouter, Outlet, Route, Routes, useLocation } from "react-router-dom";
import { AuthProvider } from "./state/AuthContext";
import { CartProvider } from "./state/CartContext";
import { initTracker, startPage } from "./lib/tracker";
import Header from "./components/Header";
import Footer from "./components/Footer";
import CartDrawer from "./components/CartDrawer";
import Home from "./pages/Home";
import Shop from "./pages/Shop";
import ProductDetail from "./pages/ProductDetail";
import ForYou from "./pages/ForYou";
import Cart from "./pages/Cart";
import Login from "./pages/Login";
import Register from "./pages/Register";
import NotFound from "./pages/NotFound";

/**
 * Maps a pathname to the `page` label the events table records, and pulls the
 * product id out of a detail URL.
 *
 * Kept here rather than inside each page so a page cannot forget to be
 * instrumented, and so `product_view` is emitted from the same place as every
 * other view - the router is the only thing that knows a navigation happened.
 */
function pageContext(pathname) {
  const product = pathname.match(/^\/product\/(\d+)/);
  if (product) return { page: "product", productId: Number(product[1]) };
  if (pathname === "/") return { page: "home", productId: null };
  if (pathname.startsWith("/shop")) return { page: "catalog", productId: null };
  if (pathname.startsWith("/for-you")) return { page: "dashboard", productId: null };
  return { page: pathname.replace(/^\//, "").split("/")[0] || "page", productId: null };
}

// Module-level, not a ref: StrictMode mounts effects twice in development, and a
// component-scoped guard would still let the second mount emit a duplicate
// page_view. Harmless in production, where it simply never matches twice.
let lastTrackedPath = null;

/**
 * Turns route changes into page boundaries.
 *
 * Fires on `pathname` only, never on the query string. Changing a catalog filter
 * is not a new page: it would reset the scroll milestones mid-scroll and bury the
 * real signal under page_views. The facet itself is already recorded by
 * `trackFilter`, which is the event the agent actually reads.
 */
function RouteTracker() {
  const { pathname } = useLocation();

  useEffect(() => {
    initTracker();
  }, []);

  useEffect(() => {
    if (lastTrackedPath === pathname) return;
    lastTrackedPath = pathname;
    startPage({ path: pathname, ...pageContext(pathname) });
    // A client-side navigation keeps the old scroll offset, which lands you
    // halfway down a page you have not seen.
    window.scrollTo(0, 0);
  }, [pathname]);

  return null;
}

function Layout() {
  return (
    <div className="flex min-h-dvh flex-col">
      <Header />
      <main className="flex-1">
        <Outlet />
      </main>
      <Footer />
      <CartDrawer />
    </div>
  );
}

export default function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        <CartProvider>
          <RouteTracker />
          <Routes>
            <Route element={<Layout />}>
              <Route path="/" element={<Home />} />
              <Route path="/shop" element={<Shop />} />
              <Route path="/product/:id" element={<ProductDetail />} />
              <Route path="/for-you" element={<ForYou />} />
              <Route path="/cart" element={<Cart />} />
              <Route path="/login" element={<Login />} />
              <Route path="/register" element={<Register />} />
              <Route path="*" element={<NotFound />} />
            </Route>
          </Routes>
        </CartProvider>
      </AuthProvider>
    </BrowserRouter>
  );
}
