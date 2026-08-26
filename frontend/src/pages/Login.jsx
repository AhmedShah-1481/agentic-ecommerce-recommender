import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useAuth } from "../state/AuthContext";
import AuthShell from "../components/AuthShell";

export default function Login() {
  const { login } = useAuth();
  const navigate = useNavigate();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [pending, setPending] = useState(false);

  const onSubmit = async (event) => {
    event.preventDefault();
    setPending(true);
    setError("");
    try {
      await login(email, password);
      // Straight to the point of having an account.
      navigate("/for-you", { replace: true });
    } catch (failure) {
      setError(failure.message || "Could not sign you in.");
      setPending(false);
    }
  };

  return (
    <AuthShell
      eyebrow="Account"
      title="Welcome back"
      lede="Your edit is built from your own browsing, so it waits behind a sign-in."
      footer={
        <>
          No account yet?{" "}
          <Link to="/register" className="link-rule text-ink">
            Create one
          </Link>
          .
        </>
      }
    >
      <form onSubmit={onSubmit} className="space-y-5" noValidate>
        <div>
          <label htmlFor="email" className="eyebrow">
            Email
          </label>
          <input
            id="email"
            type="email"
            required
            autoComplete="email"
            value={email}
            onChange={(event) => setEmail(event.target.value)}
            className="field mt-2"
          />
        </div>

        <div>
          <label htmlFor="password" className="eyebrow">
            Password
          </label>
          <input
            id="password"
            type="password"
            required
            autoComplete="current-password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            className="field mt-2"
          />
        </div>

        {error && (
          <p role="alert" className="border-l-2 border-terracotta bg-clay/60 py-2 pl-3 text-[0.8125rem]">
            {error}
          </p>
        )}

        <button type="submit" disabled={pending} className="btn btn-ink w-full justify-center disabled:opacity-50">
          {pending ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </AuthShell>
  );
}
