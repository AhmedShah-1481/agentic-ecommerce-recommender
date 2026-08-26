import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { api } from "../lib/api";

/**
 * Who is signed in.
 *
 * The session lives in an httponly cookie the client cannot read, so "am I
 * signed in?" is a question only the server can answer - `GET /api/auth/me` on
 * mount, once. That endpoint answers `{user: null}` with a 200 rather than a
 * 401, so an anonymous first visit is a normal response and not a red line in
 * the console.
 *
 * `ready` exists separately from `user` because they mean different things: a
 * header that renders "Sign in" during the round trip would flash the wrong
 * state at every signed-in user on every cold load.
 */
const AuthContext = createContext(null);

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    api
      .me(controller.signal)
      .then((data) => setUser(data?.user ?? null))
      .catch(() => setUser(null)) // API down or cookie rejected: treat as anonymous
      .finally(() => setReady(true));
    return () => controller.abort();
  }, []);

  const login = useCallback(async (email, password) => {
    const data = await api.login(email, password);
    setUser(data.user);
    return data.user;
  }, []);

  const register = useCallback(async (email, password, name) => {
    const data = await api.register(email, password, name);
    setUser(data.user);
    return data.user;
  }, []);

  const logout = useCallback(async () => {
    try {
      await api.logout();
    } finally {
      // Cleared either way: the endpoint is idempotent, and a network error on
      // the way out should not leave the UI claiming a session that is gone.
      setUser(null);
    }
  }, []);

  const value = useMemo(
    () => ({ user, ready, login, register, logout }),
    [user, ready, login, register, logout]
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const context = useContext(AuthContext);
  if (!context) throw new Error("useAuth must be used inside AuthProvider");
  return context;
}
