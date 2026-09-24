import { useState } from "react";
import type { FormEvent } from "react";

import {
  useCredentials,
  useCreateCredential,
  useRevokeCredential,
  useUpdateCredential,
} from "@/api/queries/settings";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
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
    // The API 422s blank fields (min_length=1) — never send them.
    if (name.trim() === "" || provider.trim() === "" || secret.trim() === "") return;
    create.mutate(
      { name: name.trim(), provider: provider.trim(), secret },
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
    <section className="mt-10" aria-label="Provider LLM Credentials (BYOK)">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-lg font-semibold">Provider LLM Credentials (BYOK)</h2>
          <p className="mt-0.5 text-xs text-neutral-500">
            Secrets JARVIS uses to call model providers on your agents&apos; behalf —
            never shown again after saving.
          </p>
        </div>
        {canManage && (
          <Button onClick={() => setShowForm((v) => !v)}>
            {showForm ? "Cancel" : "Add credential"}
          </Button>
        )}
      </div>

      {!storageAvailable && (
        <p className="mt-4 rounded-xl border border-neutral-800/80 bg-neutral-900/50 p-4 text-sm text-neutral-400">
          BYOK storage is not configured on this backend (no master key in the
          environment) — the API returns 503 for these operations. See{" "}
          <code className="font-mono">JARVIS_CREDENTIALS_MASTER_KEY_ENV</code> in the runbook.
        </p>
      )}

      {showForm && (
        <form
          onSubmit={submit}
          className="mt-4 grid max-w-md grid-cols-2 gap-3 rounded-xl border border-neutral-800/80 bg-neutral-900/50 p-4 shadow-sm"
        >
          <label className="text-sm">
            <span className="text-neutral-400">Name</span>
            <Input
              required
              value={name}
              onChange={(e) => setName(e.target.value)}
              className="mt-1"
            />
          </label>
          <label className="text-sm">
            <span className="text-neutral-400">Provider</span>
            <Input
              required
              value={provider}
              onChange={(e) => setProvider(e.target.value)}
              placeholder="openai_compatible"
              className="mt-1"
            />
          </label>
          <label className="col-span-2 text-sm">
            <span className="text-neutral-400">Secret</span>
            <Input
              required
              type="password"
              value={secret}
              onChange={(e) => setSecret(e.target.value)}
              placeholder="sk-…"
              className="mt-1"
            />
          </label>
          <p className="col-span-2 text-xs text-neutral-500">
            Sent once, encrypted server-side (AES-GCM), never returned or logged.
          </p>
          <Button type="submit" disabled={create.isPending} className="justify-self-start">
            {create.isPending ? "Saving…" : "Save credential"}
          </Button>
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
                    <Badge variant="danger">revoked</Badge>
                  ) : (
                    <Badge variant="success" dot>active</Badge>
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
                      <Input
                        required
                        type="password"
                        value={replacement}
                        onChange={(e) => setReplacement(e.target.value)}
                        placeholder="new secret"
                        className="w-44"
                      />
                      <Button
                        type="submit"
                        variant="ghost"
                        size="sm"
                        disabled={update.isPending}
                        className="text-sm"
                      >
                        save
                      </Button>
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => {
                          setReplacing(null);
                          setReplacement("");
                        }}
                        className="text-sm text-neutral-500"
                      >
                        cancel
                      </Button>
                    </form>
                  ) : (
                    canManage &&
                    !credential.revoked_at && (
                      <>
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={() => {
                            setReplacing(credential.id);
                            setReplacement("");
                          }}
                          className="text-sm"
                        >
                          replace secret
                        </Button>
                        <Button
                          variant="ghost"
                          size="sm"
                          disabled={revoke.isPending}
                          onClick={() => {
                            if (!window.confirm(`Revoke credential "${credential.name}"? Agents bound to it will fail runs with a model error.`))
                              return;
                            revoke.mutate(credential.id, {
                              onError: (err) => toast("error", err.message),
                            });
                          }}
                          className="ml-2 text-sm text-red-400 hover:bg-rose-950/40 hover:text-red-300"
                        >
                          revoke
                        </Button>
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