import { useState } from "react";

import type { CredentialRef } from "@/api/queries/mcp";
import type { CredentialOut } from "@/api/queries/settings";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";

// Header/env refs editor (ADR 0013): a controlled repeater over the
// config's credential refs. Values are REFERENCES only — an env-var name
// or a stored credential id; the secret itself is sent once to
// /v1/credentials, encrypted server-side, and never shown again.

export type HeaderRefs = Record<string, CredentialRef>;

/** Submit guard: blank header names or blank refs are drafts, never sent. */
export function headerRefsComplete(refs: HeaderRefs): boolean {
  return Object.entries(refs).every(
    ([header, ref]) =>
      header.trim() !== "" &&
      (ref.type === "env" ? ref.env_var.trim() !== "" : ref.credential_id.trim() !== ""),
  );
}

const selectClass =
  "mt-1 w-full cursor-pointer rounded-md border border-neutral-800 bg-neutral-900/70 px-3 py-1.5 text-sm text-neutral-100 transition-colors focus:border-indigo-500 focus:outline-none focus:ring-2 focus:ring-indigo-500/30";

interface HeaderRefsEditorProps {
  value: HeaderRefs;
  onChange: (next: HeaderRefs) => void;
  credentials: CredentialOut[];
  /** Signed-in + master key configured — otherwise the stored option is
   * absent from the select entirely (never shown-disabled). */
  allowStored: boolean;
  onInlineCreate: (name: string, secret: string) => void;
  creatingCredential: boolean;
  createError: string | null;
}

export function HeaderRefsEditor({
  value,
  onChange,
  credentials,
  allowStored,
  onInlineCreate,
  creatingCredential,
  createError,
}: HeaderRefsEditorProps) {
  const entries = Object.entries(value);
  const [credentialDraft, setCredentialDraft] = useState<{ name: string; secret: string } | null>(
    null,
  );

  const setAt = (index: number, header: string, ref: CredentialRef) => {
    const next: HeaderRefs = {};
    entries.forEach(([h, r], i) => {
      next[i === index ? header : h] = i === index ? ref : r;
    });
    onChange(next);
  };

  return (
    <div className="flex flex-col gap-2">
      {entries.map(([header, ref], index) => (
        <div key={index} className="grid grid-cols-[1fr_auto_1.2fr_auto] items-end gap-2">
          <label className="text-xs">
            <span className="text-neutral-400">Header</span>
            <Input
              value={header}
              aria-label={`Header name ${index + 1}`}
              onChange={(e) => setAt(index, e.target.value, ref)}
              placeholder="Authorization"
              className="mt-1"
            />
          </label>
          <label className="text-xs">
            <span className="text-neutral-400">Value from</span>
            <select
              aria-label={`Value source for header ${index + 1}`}
              value={ref.type}
              onChange={(e) =>
                setAt(
                  index,
                  header,
                  e.target.value === "stored"
                    ? { type: "stored", credential_id: credentials[0]?.id ?? "" }
                    : { type: "env", env_var: "" },
                )
              }
              className={selectClass}
            >
              <option value="env">Environment variable</option>
              {allowStored && <option value="stored">Stored credential</option>}
            </select>
          </label>
          {ref.type === "env" ? (
            <label className="text-xs">
              <span className="text-neutral-400">Env var name</span>
              <Input
                value={ref.env_var}
                aria-label={`Env variable for header ${index + 1}`}
                onChange={(e) => setAt(index, header, { type: "env", env_var: e.target.value })}
                placeholder="WEBZ_MCP_TOKEN"
                className="mt-1 font-mono"
              />
            </label>
          ) : (
            <label className="text-xs">
              <span className="text-neutral-400">Credential (value never shown)</span>
              <select
                aria-label={`Credential for header ${index + 1}`}
                value={ref.credential_id}
                onChange={(e) => setAt(index, header, { type: "stored", credential_id: e.target.value })}
                className={selectClass}
              >
                <option value="">Pick a credential…</option>
                {credentials
                  .filter((c) => !c.revoked_at)
                  .map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.name}
                    </option>
                  ))}
              </select>
            </label>
          )}
          <Button
            variant="ghost"
            size="sm"
            aria-label={`Remove header ${index + 1}`}
            onClick={() => {
              const next: HeaderRefs = {};
              entries.forEach(([h, r], i) => {
                if (i !== index) next[h] = r;
              });
              onChange(next);
            }}
            className="self-end pb-1.5 text-xs text-red-400 hover:bg-rose-950/40 hover:text-red-300"
          >
            Remove
          </Button>
        </div>
      ))}

      {allowStored && entries.some(([, ref]) => ref.type === "stored") && (
        <div className="flex flex-col gap-1 rounded-xl border border-neutral-800/80 bg-neutral-950 p-2">
          {credentialDraft === null ? (
            <Button
              variant="ghost"
              size="sm"
              onClick={() => setCredentialDraft({ name: "", secret: "" })}
              className="self-start text-xs"
            >
              ＋ New credential
            </Button>
          ) : (
            <div className="flex items-end gap-2">
              <label className="text-xs">
                <span className="text-neutral-400">Credential name</span>
                <Input
                  value={credentialDraft.name}
                  aria-label="New credential name"
                  onChange={(e) => setCredentialDraft({ ...credentialDraft, name: e.target.value })}
                  placeholder="webz-key"
                  className="mt-1"
                />
              </label>
              <label className="text-xs">
                <span className="text-neutral-400">Secret (sent once)</span>
                <Input
                  type="password"
                  value={credentialDraft.secret}
                  aria-label="New credential secret"
                  onChange={(e) =>
                    setCredentialDraft({ ...credentialDraft, secret: e.target.value })
                  }
                  placeholder="Bearer sk-…"
                  className="mt-1"
                />
              </label>
              <Button
                size="sm"
                disabled={creatingCredential}
                onClick={() => onInlineCreate(credentialDraft.name.trim(), credentialDraft.secret)}
              >
                {creatingCredential ? "Saving…" : "Save credential"}
              </Button>
              <Button
                variant="ghost"
                size="sm"
                onClick={() => setCredentialDraft(null)}
                className="text-xs"
              >
                Cancel
              </Button>
            </div>
          )}
          {createError !== null && (
            <p role="alert" className="text-xs text-red-400">
              {createError}
            </p>
          )}
        </div>
      )}

      {!allowStored && entries.length > 0 && (
        <p className="text-xs text-neutral-500">
          Storing API keys as encrypted credentials requires signing in — in
          anonymous mode use environment variables (values in <code>.env</code>).
        </p>
      )}

      <Button
        variant="ghost"
        size="sm"
        onClick={() => onChange({ ...value, "": { type: "env", env_var: "" } })}
        className="self-start text-xs"
      >
        ＋ Add header
      </Button>
    </div>
  );
}