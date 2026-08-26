import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useAuth } from "../state/AuthContext";
import AuthShell from "../components/AuthShell";

export default function Register() {
  const { register } = useAuth();
  const navigate = useNavigate();
  const [form, setForm] = useState({ name: "", email: "", password: "" });
  const [error, setError] = useState("");
  const [pending, setPending] = useState(false);

  const set = (field) => (event) => setForm({ ...form, [field]: event.target.value });

  const onSubmit = async (event) => {
    event.preventDefault();
    setPending(true);
    setError("");
    try {
      await register(form.email, form.password, form.name);
      navigate("/shop", { replace: true });
    } catch (failure) {
      setError(failure.message || "Could not create the account.");
      setPending(false);
    }
  };

  return (
    <AuthShell
      eyebrow="Account"
      title="Start a signal"
      lede="An account gives the agent somewhere to keep what it notices. Browse for a minute afterwards and the first edit writes itself."
      footer={
        <>
          Already have one?{" "}
          <Link to="/login" className="link-rule text-ink">
            Sign in
          </Link>
          .
        </>
      }
    >
      <form onSubmit={onSubmit} className="space-y-5" noValidate>
        <div>
          <label htmlFor="name" className="eyebrow">
            Name
          </label>
          <input
            id="name"
            type="text"
            autoComplete="name"
            value={form.name}
            onChange={set("name")}
            className="field mt-2"
          />
        </div>

        <div>
          <label htmlFor="email" className="eyebrow">
            Email
          </label>
          <input
            id="email"
            type="email"
            required
            autoComplete="email"
            value={form.email}
            onChange={set("email")}
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
            minLength={8}
            autoComplete="new-password"
            value={form.password}
            onChange={set("password")}
            className="field mt-2"
          />
          <p className="mt-2 text-[0.75rem] text-stone-light">At least eight characters.</p>
        </div>

        {error && (
          <p role="alert" className="border-l-2 border-terracotta bg-clay/60 py-2 pl-3 text-[0.8125rem]">
            {error}
          </p>
        )}

        <button type="submit" disabled={pending} className="btn btn-ink w-full justify-center disabled:opacity-50">
          {pending ? "Creating…" : "Create account"}
        </button>
      </form>
    </AuthShell>
  );
}
