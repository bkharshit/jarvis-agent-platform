import { useState } from "react";
import type { FormEvent } from "react";

import { useApiKeys, useCreateApiKey, useRevokeApiKey } from "@/api/queries/settings";
import type { ApiKeyCreated } from "@/api/queries/settings";
import { toast } from "@/stores/toast";

// API keys (S2) — the plaintext is returned exactly once, at create, held
// in page state only; no GET ever returns it again (ADR 0009 §7).

export function ApiKeysPanel({ canCreate }: { canCreate: boolean }) {
  const { data: keys, isPending, isError, error } = useApiKeys();
  const create = useCreateApiKey();
  const revoke = useRevokeApiKey();
  const [created, setCreated] = useState<ApiKeyCreated | null>(null);
  const [name, setName] = useState("");

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const trimmed = name.trim();
    if (trimmed === "") return; // the API 422s blank names — never send it
    create.mutate(
      { name: trimmed },
      {
        onSuccess: (created) => {
          setCreated(created); // shown once, below
          setName("");
        },
        onError: (err) => toast("error", err.message),
      },
    );
  };

  return (
    <section className="mt-10" aria-label="JARVIS API keys">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-lg font-semibold">JARVIS API keys</h2>
          <p className="mt-0.5 text-xs text-neutral-500">
            Authenticate to this platform&apos;s API — scripts, CI, the CLI.
          </p>
        </div>
        {canCreate && (
          <form onSubmit={submit} className="flex items-center gap-2">
            <input
              required
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="key name"
              className="rounded border border-neutral-700 bg-neutral-900 px-2 py-1.5 text-sm"
            />
            <button
              type="submit"
              disabled={create.isPending || name.trim() === ""}
              className="cursor-pointer rounded bg-neutral-100 px-3 py-1.5 text-sm font-medium text-neutral-900 hover:bg-white disabled:cursor-not-allowed disabled:text-neutral-500"
            >
              {create.isPending ? "Creating…" : "Create key"}
            </button>
          </form>
        )}
      </div>

      {created && (
        <div className="mt-4 rounded border border-amber-700/50 bg-amber-950/30 p-4" role="status">
          <p className="text-sm font-medium text-amber-200">
            Key “{created.name}” created — copy it now.
          </p>
          <code className="mt-2 block break-all font-mono text-sm text-neutral-100">
            {created.plaintext}
          </code>
          <p className="mt-2 text-xs text-neutral-400">
            This is the only time the plaintext is shown. It is stored only as a
            hash and cannot be recovered.
          </p>
          <button
            type="button"
            onClick={() => setCreated(null)}
            className="mt-3 cursor-pointer text-sm text-neutral-400 hover:text-neutral-200"
          >
            Done — I saved it
          </button>
        </div>
      )}

      {isPending ? (
        <p className="mt-4 text-sm text-neutral-400">Loading keys…</p>
      ) : isError ? (
        <p className="mt-4 text-sm text-red-400">{error.message}</p>
      ) : keys.length === 0 ? (
        <p className="mt-4 text-sm text-neutral-400">No API keys yet.</p>
      ) : (
        <table className="mt-4 w-full text-left text-sm">
          <thead className="text-neutral-400">
            <tr className="border-b border-neutral-800">
              <th className="py-2 pr-4 font-medium">Name</th>
              <th className="py-2 pr-4 font-medium">Prefix</th>
              <th className="py-2 pr-4 font-medium">Last used</th>
              <th className="py-2 pr-4 font-medium">Status</th>
              <th className="py-2 pr-4 text-right font-medium">Actions</th>
            </tr>
          </thead>
          <tbody>
            {keys.map((key) => (
              <tr key={key.id} className="border-b border-neutral-900">
                <td className="py-2 pr-4 text-neutral-100">{key.name}</td>
                <td className="py-2 pr-4 font-mono text-xs text-neutral-400">
                  {key.key_prefix}…
                </td>
                <td className="py-2 pr-4 text-neutral-400">
                  {key.last_used_at ? new Date(key.last_used_at).toLocaleString() : "never"}
                </td>
                <td className="py-2 pr-4">
                  {key.revoked_at ? (
                    <span className="text-red-400">revoked</span>
                  ) : (
                    <span className="text-green-500">active</span>
                  )}
                </td>
                <td className="py-2 pr-4 text-right">
                  {canCreate && !key.revoked_at && (
                    <button
                      type="button"
                      disabled={revoke.isPending}
                      onClick={() => {
                        if (!window.confirm(`Revoke key "${key.name}"? Clients using it stop working.`))
                          return;
                        revoke.mutate(key.id, {
                          onError: (err) => toast("error", err.message),
                        });
                      }}
                      className="cursor-pointer text-red-400 hover:text-red-300 disabled:cursor-not-allowed disabled:text-neutral-600"
                    >
                      revoke
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}