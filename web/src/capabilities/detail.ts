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