// Typed accessors into the capabilities `detail` blocks. The payload's
// detail dicts are `unknown` at the type level (they are derived facts);
// these helpers validate shape at runtime so pages never cast blindly.

import type { Capabilities } from "./types";

function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null ? (value as Record<string, unknown>) : null;
}

function stringList(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((v): v is string => typeof v === "string") : [];
}

export interface BuiltinTool {
  name: string;
  description: string;
}

export function strategyNames(capabilities: Capabilities | undefined): string[] {
  const detail = asRecord(capabilities?.sections.agents?.detail);
  return detail ? stringList(detail.strategies) : [];
}

export function builtinTools(capabilities: Capabilities | undefined): BuiltinTool[] {
  const detail = asRecord(capabilities?.sections.tools?.detail);
  if (!detail || !Array.isArray(detail.builtins)) return [];
  return detail.builtins.flatMap((entry) => {
    const record = asRecord(entry);
    return record && typeof record.name === "string"
      ? [{ name: record.name, description: typeof record.description === "string" ? record.description : "" }]
      : [];
  });
}

export function providerNames(capabilities: Capabilities | undefined): string[] {
  const detail = asRecord(capabilities?.sections.models?.detail);
  if (!detail || !Array.isArray(detail.providers)) return [];
  return detail.providers.flatMap((entry) => {
    const record = asRecord(entry);
    return record && typeof record.name === "string" ? [record.name] : [];
  });
}
export interface BuiltinToolFull {
  name: string;
  description: string;
  parameters: Record<string, unknown>;
}

export interface McpGate {
  enabled: boolean;
  stage: string | null;
}

export function mcpGate(capabilities: Capabilities | undefined): McpGate {
  const detail = asRecord(capabilities?.sections.tools?.detail);
  const mcp = detail ? asRecord(detail.mcp) : null;
  return {
    enabled: mcp?.enabled === true,
    stage: typeof mcp?.stage === "string" ? mcp.stage : null,
  };
}

export function builtinToolsFull(capabilities: Capabilities | undefined): BuiltinToolFull[] {
  const detail = asRecord(capabilities?.sections.tools?.detail);
  if (!detail || !Array.isArray(detail.builtins)) return [];
  return detail.builtins.flatMap((entry) => {
    const record = asRecord(entry);
    if (!record || typeof record.name !== "string") return [];
    return [
      {
        name: record.name,
        description: typeof record.description === "string" ? record.description : "",
        parameters:
          asRecord(record.parameters) ?? {},
      },
    ];
  });
}

export interface ProviderInfo {
  name: string;
  description: string;
  capabilities: Record<string, unknown>;
}

export interface ModelDefaults {
  provider: string;
  model: string;
  base_url: string | null;
}

export function modelProviders(capabilities: Capabilities | undefined): ProviderInfo[] {
  const detail = asRecord(capabilities?.sections.models?.detail);
  if (!detail || !Array.isArray(detail.providers)) return [];
  return detail.providers.flatMap((entry) => {
    const record = asRecord(entry);
    if (!record || typeof record.name !== "string") return [];
    return [
      {
        name: record.name,
        description: typeof record.description === "string" ? record.description : "",
        capabilities: asRecord(record.capabilities) ?? {},
      },
    ];
  });
}

export function modelDefaults(capabilities: Capabilities | undefined): ModelDefaults | null {
  const detail = asRecord(capabilities?.sections.models?.detail);
  const defaults = detail ? asRecord(detail.defaults) : null;
  if (!defaults || typeof defaults.provider !== "string") return null;
  return {
    provider: defaults.provider,
    model: typeof defaults.model === "string" ? defaults.model : "",
    base_url: typeof defaults.base_url === "string" ? defaults.base_url : null,
  };
}

export interface SettingsFacts {
  /** "anonymous" (local single-user) | "required" — a backend config fact. */
  authMode: string;
  /** Whether BYOK storage (the master key) is configured on the backend. */
  credentialsAvailable: boolean;
}

export function settingsFacts(capabilities: Capabilities | undefined): SettingsFacts {
  const detail = asRecord(capabilities?.sections.settings?.detail);
  const credentials = detail ? asRecord(detail.credentials) : null;
  return {
    authMode: detail && typeof detail.auth_mode === "string" ? detail.auth_mode : "anonymous",
    credentialsAvailable: credentials?.available === true,
  };
}

/** S10: human-in-the-loop is live on the backend — pause frames, the
 * resume route, and the awaiting-input inbox are real. */
export function humanInTheLoop(capabilities: Capabilities | undefined): boolean {
  const detail = asRecord(capabilities?.sections.executions?.detail);
  return detail?.human_in_the_loop === true;
}
