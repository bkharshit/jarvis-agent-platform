import { useState } from "react";
import type { FormEvent } from "react";

import {
  useCredentials,
  useCreateCredential,
  useRevokeCredential,
  useUpdateCredential,
} from "@/api/queries/settings";
import { toast } from "@/stores/toast";

// BYOK credentials (S2, ADR 0006) — write-only through the API: the secret
// is sent once on create/update and never comes back in any response. What
// the table shows is metadata only.

export function CredentialsPanel({
  canManage,
  storageAvailable,
}: {
  canManage: boolean;
  storageAvailable: boolean;
}) {
  const { data: credentials, isPending, isError, error } = useCredentials();
  const create = useCreateCredential();
  const update = useUpdateCredential();
  const revoke = useRevokeCredential();
  const [showForm, setShowForm] = useState(false);
  const [name, setName] = useState("");
  const [provider, setProvider] = useState("");
  const [secret, setSecret] = useState("");
  // Per-row secret replacement fields (credential ids are the row keys).
  const [replacing, setReplacing] = useState<string | null>(null);
  const [replacement, setReplacement] = useState("");

  const submit = (event: FormEvent) => {
    event.preventDefault();
    create.mutate(
      { name, provider, secret },
      {
        onSuccess: () => {
          setShowForm(false);
          setName("");
          setProvider("");
          setSecret("");
        },
        onError: (err) => toast("error", err.message),
      },
    );
  };

  const replaceSecret = (credentialId: string) => {
    update.mutate(
      { credentialId, body: { secret: replacement } },
      {
        onSuccess: () => {
          setReplacing(null);
          setReplacement("");
        },
        onError: (err) => toast("error", err.message),
      },
    );
  };

  return (
    <section className="mt-10" aria-label="Credentials">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold">Credentials</h2>
        {canManage && (
          <button
            type="button"
            onClick={() => setShowForm((v) => !v)}
            className="cursor-pointer rounded bg-neutral-100 px-3 py-1.5 text-sm font-medium text-neutral-900 hover:bg-white"
          >
            {showForm ? "Cancel" : "Add credential"}
          </button>
        )}
      </div>

      {!storageAvailable && (
        <p className="mt-4 rounded border border-neutral-800 p-4 text-sm text-neutral-400">
          BYOK storage is not configured on this backend (no master key in the
          environment) — the API returns 503 for these operations. See{" "}
          <code className="font-mono">JARVIS_CREDENTIALS_MASTER_KEY_ENV</code> in the runbook.
        </p>
      )}

      {showForm && (
        <form onSubmit={submit} className="mt-4 grid max-w-md grid-cols-2 gap-3 rounded border border-neutral-800 bg-neutral-900 p-4">
          <label className="text-sm">
            <span className="text-neutral-400">Name</span>
            <input
              required
              value={name}
              onChange={(e) => setName(e.target.value)}
              className="mt-1 w-full rounded border border-neutral-700 bg-neutral-950 px-2 py-1 text-sm"
            />
          </label>
          <label className="text-sm">
            <span className="text-neutral-400">Provider</span>
            <input
              required
              value={provider}
              onChange={(e) => setProvider(e.target.value)}
              placeholder="openai_compatible"
              className="mt-1 w-full rounded border border-neutral-700 bg-neutral-950 px-2 py-1 text-sm"
            />
          </label>
          <label className="col-span-2 text-sm">
            <span className="text-neutral-400">Secret</span>
            <input
              required
              type="password"
              value={secret}
              onChange={(e) => setSecret(e.target.value)}
              placeholder="sk-…"
              className="mt-1 w-full rounded border border-neutral-700 bg-neutral-950 px-2 py-1 text-sm"
            />
          </label>
          <p className="col-span-2 text-xs text-neutral-500">
            Sent once, encrypted server-side (AES-GCM), never returned or logged.
          </p>
          <button
            type="submit"
            disabled={create.isPending}
            className="cursor-pointer justify-self-start rounded bg-neutral-100 px-3 py-1.5 text-sm font-medium text-neutral-900 hover:bg-white disabled:cursor-not-allowed disabled:text-neutral-500"
          >
            {create.isPending ? "Saving…" : "Save credential"}
          </button>
          {create.isError && (
            <p className="text-sm text-red-400" role="alert">
              {create.error.message}
            </p>
          )}
        </form>
      )}

      {isPending ? (
        <p className="mt-4 text-sm text-neutral-400">Loading credentials…</p>
      ) : isError ? (
        <p className="mt-4 text-sm text-red-400">{error.message}</p>
      ) : credentials.length === 0 ? (
        <p className="mt-4 text-sm text-neutral-400">
          No credentials yet. Agents reference these by id as a{" "}
          <code className="font-mono">stored</code> credential ref.
        </p>
      ) : (
        <table className="mt-4 w-full text-left text-sm">
          <thead className="text-neutral-400">
            <tr className="border-b border-neutral-800">
              <th className="py-2 pr-4 font-medium">Name</th>
              <th className="py-2 pr-4 font-medium">Provider</th>
              <th className="py-2 pr-4 font-medium">Status</th>
              <th className="py-2 pr-4 text-right font-medium">Actions</th>
            </tr>
          </thead>
          <tbody>
            {credentials.map((credential) => (
              <tr key={credential.id} className="border-b border-neutral-900">
                <td className="py-2 pr-4 text-neutral-100">{credential.name}</td>
                <td className="py-2 pr-4 font-mono text-xs text-neutral-400">
                  {credential.provider}
                </td>
                <td className="py-2 pr-4">
                  {credential.revoked_at ? (
                    <span className="text-red-400">revoked</span>
                  ) : (
                    <span className="text-green-500">active</span>
                  )}
                </td>
                <td className="py-2 pr-4 text-right">
                  {replacing === credential.id ? (
                    <form
                      onSubmit={(e) => {
                        e.preventDefault();
                        replaceSecret(credential.id);
                      }}
                      className="inline-flex items-center gap-2"
                    >
                      <input
                        required
                        type="password"
                        value={replacement}
                        onChange={(e) => setReplacement(e.target.value)}
                        placeholder="new secret"
                        className="rounded border border-neutral-700 bg-neutral-900 px-2 py-1 text-sm"
                      />
                      <button
                        type="submit"
                        disabled={update.isPending}
                        className="cursor-pointer text-neutral-300 hover:text-neutral-100"
                      >
                        save
                      </button>
                      <button
                        type="button"
                        onClick={() => {
                          setReplacing(null);
                          setReplacement("");
                        }}
                        className="cursor-pointer text-neutral-500 hover:text-neutral-300"
                      >
                        cancel
                      </button>
                    </form>
                  ) : (
                    canManage &&
                    !credential.revoked_at && (
                      <>
                        <button
                          type="button"
                          onClick={() => {
                            setReplacing(credential.id);
                            setReplacement("");
                          }}
                          className="cursor-pointer text-neutral-400 hover:text-neutral-200"
                        >
                          replace secret
                        </button>
                        <button
                          type="button"
                          disabled={revoke.isPending}
                          onClick={() => {
                            if (!window.confirm(`Revoke credential "${credential.name}"? Agents bound to it will fail runs with a model error.`))
                              return;
                            revoke.mutate(credential.id, {
                              onError: (err) => toast("error", err.message),
                            });
                          }}
                          className="ml-3 cursor-pointer text-red-400 hover:text-red-300 disabled:cursor-not-allowed disabled:text-neutral-600"
                        >
                          revoke
                        </button>
                      </>
                    )
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