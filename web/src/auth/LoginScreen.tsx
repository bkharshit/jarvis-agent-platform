import { useState } from "react";
import type { FormEvent } from "react";

import { useLogin } from "@/api/queries/auth";

// The sign-in surface (S2). Shown by the Settings section when the backend
// runs JARVIS_AUTH_MODE=required and no principal is acting; the session
// cookie it establishes is same-origin, so the API client needs nothing.

export function LoginScreen() {
  const login = useLogin();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");

  const submit = (event: FormEvent) => {
    event.preventDefault();
    login.mutate({ email, password });
  };

  return (
    <div className="mx-auto max-w-sm px-6 py-16">
      <h1 className="text-xl font-semibold">Sign in</h1>
      <p className="mt-1 text-sm text-neutral-400">
        This JARVIS instance requires authentication.
      </p>

      <form onSubmit={submit} className="mt-6 flex flex-col gap-3">
        <label className="text-sm">
          <span className="text-neutral-400">Email</span>
          <input
            type="email"
            required
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            className="mt-1 w-full rounded border border-neutral-700 bg-neutral-900 px-3 py-1.5 text-sm text-neutral-100 focus:border-neutral-500 focus:outline-none"
          />
        </label>
        <label className="text-sm">
          <span className="text-neutral-400">Password</span>
          <input
            type="password"
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className="mt-1 w-full rounded border border-neutral-700 bg-neutral-900 px-3 py-1.5 text-sm text-neutral-100 focus:border-neutral-500 focus:outline-none"
          />
        </label>

        {login.isError && (
          <p className="text-sm text-red-400" role="alert">
            {login.error.message}
          </p>
        )}

        <button
          type="submit"
          disabled={login.isPending}
          className="mt-2 cursor-pointer rounded bg-neutral-100 px-3 py-1.5 text-sm font-medium text-neutral-900 hover:bg-white disabled:cursor-not-allowed disabled:text-neutral-500"
        >
          {login.isPending ? "Signing in…" : "Sign in"}
        </button>
      </form>

      <p className="mt-6 text-xs text-neutral-500">
        No account? The first owner is provisioned with the CLI:
        <code className="ml-1 font-mono">jarvis tenant create / user create / api-key create</code>
      </p>
    </div>
  );
}