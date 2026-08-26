/**
 * The one place that talks to the FastAPI backend.
 *
 * Two things every call depends on:
 *
 *   - `credentials: "include"`. The API authenticates with the same signed,
 *     httponly session cookie the Jinja admin uses. There is no token in
 *     localStorage to read, by design - the storefront cannot see its own
 *     session, it can only send it. Omitting this flag makes every signed-in
 *     endpoint answer 401 with no other symptom.
 *
 *   - An absolute origin. The storefront is served from :5173 and the API from
 *     :8000, so requests are cross-origin and answered by the CORS middleware.
 *     They are still same-*site* (a port is not part of a site), which is what
 *     lets a SameSite=Lax cookie ride along in development.
 *
 * Errors arrive as `ApiError` carrying the status, because the difference
 * between 401 and 404 is a different piece of UI, not a different message.
 */

const BASE = (import.meta.env.VITE_API_URL || "http://localhost:8000").replace(/\/$/, "");

export class ApiError extends Error {
  constructor(message, status, payload) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.payload = payload;
  }
}

async function request(path, { method = "GET", body, signal } = {}) {
  let response;
  try {
    response = await fetch(`${BASE}${path}`, {
      method,
      signal,
      credentials: "include",
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch (error) {
    // An aborted request is a component unmounting or a filter changing, not a
    // failure - it must not reach the UI as an error banner.
    if (error.name === "AbortError") throw error;
    throw new ApiError("Could not reach the store. Is the API running?", 0);
  }

  // 204 and the beacon endpoints have no body worth parsing.
  const text = await response.text();
  let payload = null;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = null;
    }
  }

  if (!response.ok) {
    // FastAPI puts the human-readable message in `detail`, and the register and
    // login handlers deliberately write it for display, so it is shown as-is.
    const detail = payload && typeof payload.detail === "string" ? payload.detail : null;
    throw new ApiError(detail || `Request failed (${response.status})`, response.status, payload);
  }

  return payload;
}

/** Drops empty values so the URL carries only filters that are actually set. */
function query(params) {
  const search = new URLSearchParams();
  Object.entries(params || {}).forEach(([key, value]) => {
    if (value === undefined || value === null || value === "") return;
    search.set(key, String(value));
  });
  const encoded = search.toString();
  return encoded ? `?${encoded}` : "";
}

export const api = {
  // ---- Catalog (public) ----
  catalog: (params, signal) => request(`/api/catalog${query(params)}`, { signal }),
  facets: (signal) => request("/api/catalog/facets", { signal }),
  product: (id, signal) => request(`/api/products/${id}`, { signal }),

  // ---- Auth ----
  me: (signal) => request("/api/auth/me", { signal }),
  login: (email, password) => request("/api/auth/login", { method: "POST", body: { email, password } }),
  register: (email, password, name) =>
    request("/api/auth/register", { method: "POST", body: { email, password, name } }),
  logout: () => request("/api/auth/logout", { method: "POST" }),

  // ---- Recommendations (session-scoped; 401 when signed out) ----
  recommendations: (signal) => request("/api/recommendations", { signal }),
  refreshRecommendations: () => request("/api/recommendations/refresh", { method: "POST" }),
  signal: (limit, signal) => request(`/api/me/signal${query({ limit })}`, { signal }),
};

export { BASE as API_BASE };
