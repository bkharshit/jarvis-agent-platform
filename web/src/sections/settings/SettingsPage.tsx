import { useWhoami, useLogout } from "@/api/queries/auth";
import { SectionGate } from "@/capabilities/SectionGate";
import { settingsFacts } from "@/capabilities/detail";
import { useCapabilities } from "@/capabilities/useCapabilities";

import { LoginScreen } from "@/auth/LoginScreen";
import { ApiKeysPanel } from "./ApiKeysPanel";
import { CredentialsPanel } from "./CredentialsPanel";
import { MembersPanel } from "./MembersPanel";

// Settings (S2) — auth facts, members, API keys, BYOK credentials.
//
// The page branches on two backend facts, never on a hardcoded mode:
// 1. capabilities.settings.detail.auth_mode — "required" + no acting
//    principal renders the LoginScreen (the 401 funnel: whoami resolves
//    null there, and every panel's API error is shown verbatim otherwise).
// 2. "anonymous" mode is the single-user local default — signed-in-ness is
//    disabled by configuration and the notice says so, honestly.

function WhoamiHeader() {
  const { data: whoami } = useWhoami();
  const logout = useLogout();

  if (!whoami) return null;

  const isAnonymous = whoami.mode === "anonymous";

  return (
    <div className="rounded border border-neutral-800 bg-neutral-900 p-4">
      <div className="flex items-start justify-between">
        <div>
          <p className="text-sm text-neutral-100">
            {isAnonymous
              ? "Anonymous principal (local mode)"
              : (whoami.email ?? whoami.user_id ?? "signed in")}
          </p>
          <p className="mt-1 text-xs text-neutral-400">
            tenant <span className="font-mono">{whoami.tenant_id}</span>
            {" · mode "}
            <span className="font-mono">{whoami.mode}</span>
            {whoami.role !== null && (
              <>
                {" · role "}
                <span className="font-mono">{whoami.role}</span>
              </>
            )}
          </p>
        </div>
        {!isAnonymous && (
          <button
            type="button"
            disabled={logout.isPending}
            onClick={() => logout.mutate()}
            className="cursor-pointer rounded border border-neutral-700 px-3 py-1.5 text-sm text-neutral-300 hover:bg-neutral-800 disabled:cursor-not-allowed disabled:text-neutral-600"
          >
            Sign out
          </button>
        )}
      </div>
    </div>
  );
}

function SettingsInner() {
  const { data: capabilities } = useCapabilities();
  const { data: whoami, isPending: whoamiPending } = useWhoami();
  const facts = settingsFacts(capabilities);

  // auth_mode=required and nobody is acting → the sign-in surface.
  if (facts.authMode === "required" && !whoamiPending && whoami === null) {
    return <LoginScreen />;
  }

  // Member management is an admin/owner API gate (ADR 0009 §8); keys and
  // credentials belong to users — the anonymous principal can own neither,
  // so the panels stay but their mutating controls only render for a
  // signed-in user. Anonymous mode says this out loud:
  const signedIn = whoami != null && whoami.mode !== "anonymous";
  const canManage =
    signedIn && (whoami?.role === "owner" || whoami?.role === "admin");

  return (
    <div className="mx-auto max-w-4xl px-6 py-8">
      <h1 className="text-xl font-semibold">Settings</h1>
      <p className="mt-1 text-sm text-neutral-400">
        Authentication, tenant members, JARVIS API keys, and provider LLM credentials (BYOK).
      </p>

      {facts.authMode === "anonymous" && (
        <p className="mt-4 rounded border border-neutral-800 bg-neutral-900 p-4 text-sm text-neutral-400" role="note">
          Single-user local mode — this backend runs{" "}
          <code className="font-mono">JARVIS_AUTH_MODE=anonymous</code>, so every
          request acts on the default tenant without signing in. Member
          management, API keys, and credentials require a real principal; run{" "}
          <code className="font-mono">JARVIS_AUTH_MODE=required</code> and use the
          CLI bootstrap commands to create one.
        </p>
      )}

      <div className="mt-6">
        <WhoamiHeader />
      </div>

      {whoamiPending && <p className="mt-4 text-sm text-neutral-400">Loading session…</p>}

      <MembersPanel canManage={canManage} />
      <ApiKeysPanel canCreate={signedIn} />
      <CredentialsPanel canManage={signedIn} storageAvailable={facts.credentialsAvailable} />
    </div>
  );
}

export function SettingsPage() {
  return (
    <SectionGate sectionKey="settings">
      <SettingsInner />
    </SectionGate>
  );
}