import { useState } from "react";

import { useUpdateCredential } from "@/api/queries/settings";
import { toast } from "@/stores/toast";

// Rotate a stored credential's secret from an MCP server row (ADR 0013):
// the same write-only pattern as Settings → Credentials — the new secret
// is sent once via PATCH /v1/credentials and never comes back.

export function RotateCredentialButton({
  credentialId,
  credentialName,
}: {
  credentialId: string;
  credentialName: string;
}) {
  const update = useUpdateCredential();
  const [open, setOpen] = useState(false);
  const [secret, setSecret] = useState("");

  if (!open) {
    return (
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="cursor-pointer text-neutral-400 hover:text-neutral-200"
      >
        Rotate secret
      </button>
    );
  }

  const rotate = () => {
    if (secret.trim() === "") return; // the API 422s a blank secret — never send it
    if (
      !window.confirm(
        `Replace the secret for "${credentialName}"? The old value stops working immediately.`,
      )
    )
      return;
    update.mutate(
      { credentialId, body: { secret } },
      {
        onSuccess: () => {
          setOpen(false);
          setSecret("");
          toast("success", `Rotated the secret for ${credentialName}`);
        },
        onError: (err) => toast("error", err.message),
      },
    );
  };

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        rotate();
      }}
      className="flex items-center gap-1"
    >
      <input
        type="password"
        value={secret}
        onChange={(e) => setSecret(e.target.value)}
        aria-label={`New secret for ${credentialName}`}
        placeholder="new secret"
        className="w-40 rounded border border-neutral-700 bg-neutral-950 px-2 py-1 text-xs text-neutral-100"
        autoFocus
      />
      <button
        type="submit"
        disabled={update.isPending}
        className="cursor-pointer text-xs text-red-400 hover:text-red-300 disabled:cursor-not-allowed disabled:text-neutral-600"
      >
        {update.isPending ? "Rotating…" : "Save"}
      </button>
      <button
        type="button"
        onClick={() => {
          setOpen(false);
          setSecret("");
        }}
        className="cursor-pointer text-xs text-neutral-400 hover:text-neutral-200"
      >
        Cancel
      </button>
    </form>
  );
}