import { create } from "zustand";

import type { AgentDefinition, AgentUpsert, ToolBinding } from "@/api/queries/agents";

import type { ModelDefaults } from "@/capabilities/detail";

// Form-shaped draft. Optional API fields are "" here and *omitted* from the
// saved body — the PATCH route does model_dump(exclude_unset=True) +
// model_copy without re-validation, so an explicit null can poison the
// snapshot (plan risk: PATCH null injection). Client-only state never
// reaches the wire (every API schema is extra="forbid").

type ModelDefaultsInput = ModelDefaults;

export interface ToolDraft {
  name: string;
  enabled: boolean;
  config: string; // JSON text; "" = {}
}

/** Which credential_ref kind the model binds to; "" = no credential_ref. */
export type CredentialKind = "" | "env" | "stored";

export interface AgentDraft {
  name: string;
  description: string;
  model: {
    provider: string;
    model: string;
    base_url: string;
    credential_kind: CredentialKind;
    credential_value: string; // env var name, or stored credential id
  };
  system_prompt: string;
  user_prompt_template: string;
  tools: ToolDraft[];
  strategy: { type: "function_calling" | "react"; params: string };
  memory: { enabled: boolean; max_messages: number; session_key: string };
  max_iterations: number;
  temperature: number;
  output_schema: string; // JSON text; "" = unset
}

interface EditorState {
  draft: AgentDraft | null;
  /** id of the agent being edited, or null while creating. */
  agentId: string | null;
  isDirty: boolean;
  /** `defaults` seeds a create-draft from the capabilities env defaults. */
  open: (definition: AgentDefinition | null, defaults?: ModelDefaultsInput | null) => void;
  close: () => void;
  update: (patch: Partial<AgentDraft>) => void;
}

/** Split the credential_ref union into form fields (S2: env | stored). */
function credentialFromRef(
  ref: AgentDefinition["model"]["credential_ref"],
): { credential_kind: CredentialKind; credential_value: string } {
  if (!ref) return { credential_kind: "", credential_value: "" };
  return ref.type === "env"
    ? { credential_kind: "env", credential_value: ref.env_var }
    : { credential_kind: "stored", credential_value: ref.credential_id };
}

function draftFromDefinition(definition: AgentDefinition): AgentDraft {
  return {
    name: definition.name,
    description: definition.description,
    model: {
      provider: definition.model.provider,
      model: definition.model.model,
      base_url: definition.model.base_url ?? "",
      ...credentialFromRef(definition.model.credential_ref),
    },
    system_prompt: definition.system_prompt,
    user_prompt_template: definition.user_prompt_template ?? "",
    tools: (definition.tools ?? []).map((t) => ({
      name: t.name,
      enabled: t.enabled ?? true,
      config: JSON.stringify(t.config ?? {}, null, 2),
    })),
    strategy: {
      type: definition.strategy.type,
      params: JSON.stringify(definition.strategy.params ?? {}, null, 2),
    },
    memory: {
      enabled: definition.memory?.enabled ?? false,
      max_messages: definition.memory?.max_messages ?? 20,
      session_key: definition.memory?.session_key ?? "",
    },
    max_iterations: definition.max_iterations,
    temperature: definition.temperature,
    output_schema:
      definition.output_schema === undefined || definition.output_schema === null
        ? ""
        : JSON.stringify(definition.output_schema, null, 2),
  };
}

function draftForCreate(defaults?: ModelDefaultsInput | null): AgentDraft {
  // Seed from the environment defaults when the capabilities payload has
  // them (real, env-derived data — ADR 0007); "mock" is only the fallback
  // when no defaults exist (capabilities not loaded yet).
  const model =
    defaults && defaults.model !== ""
      ? {
          provider: defaults.provider,
          model: defaults.model,
          ...(defaults.base_url !== null && defaults.base_url !== ""
            ? { base_url: defaults.base_url }
            : {}),
        }
      : { provider: "mock", model: "mock-agent" };
  return draftFromDefinition({
    id: "",
    name: "",
    description: "",
    model,
    system_prompt: "",
    user_prompt_template: null,
    tools: [],
    strategy: { type: "function_calling", params: {} },
    memory: { enabled: false, max_messages: 20 },
    max_iterations: 8,
    temperature: 0.7,
    output_schema: null,
  });
}

/** Parse a JSON textarea; null keeps the field absent from the payload. */
function parseJsonField(text: string): { error?: string; value?: unknown } {
  if (text.trim() === "") return {};
  try {
    const parsed: unknown = JSON.parse(text);
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
      return { error: "must be a JSON object" };
    }
    return { value: parsed };
  } catch {
    return { error: "invalid JSON" };
  }
}

function bindingFromDraft(tool: ToolDraft): { error?: string; binding?: ToolBinding } {
  let config: Record<string, unknown> = {};
  if (tool.config.trim() !== "") {
    try {
      const parsed: unknown = JSON.parse(tool.config);
      if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
        return { error: `${tool.name}: config must be a JSON object` };
      }
      config = parsed as Record<string, unknown>;
    } catch {
      return { error: `${tool.name}: invalid JSON config` };
    }
  }
  return { binding: { name: tool.name, enabled: tool.enabled, config } };
}

export interface SavePayload {
  body: AgentUpsert;
  error?: string;
}

/**
 * Build the save body: every key the API treats as optional is *omitted*
 (never null, never undefined) when the draft left it empty.
 */
export function toSavePayload(draft: AgentDraft): SavePayload {
  if (!draft.name.trim()) return { body: {}, error: "name is required" };

  const strategyParams = parseJsonField(draft.strategy.params);
  if (strategyParams.error) return { body: {}, error: `strategy params: ${strategyParams.error}` };
  const outputSchema = parseJsonField(draft.output_schema);
  if (outputSchema.error) return { body: {}, error: `output schema: ${outputSchema.error}` };

  const bindings: ToolBinding[] = [];
  for (const tool of draft.tools) {
    const result = bindingFromDraft(tool);
    if (result.error) return { body: {}, error: result.error };
    bindings.push(result.binding!);
  }

  // credential_ref (S2 union): "" kind = omitted entirely; a kind with an
  // empty value is a form error, not a half-written ref.
  const credentialKind = draft.model.credential_kind;
  const credentialValue = draft.model.credential_value.trim();
  if (credentialKind !== "" && credentialValue === "") {
    return {
      body: {},
      error:
        credentialKind === "env"
          ? "credential: name the environment variable"
          : "credential: paste the stored credential id (Settings → Provider LLM Credentials)",
    };
  }
  const credentialRef =
    credentialKind === "env"
      ? ({ type: "env", env_var: credentialValue } as const)
      : credentialKind === "stored"
        ? ({ type: "stored", credential_id: credentialValue } as const)
        : undefined;

  const body: AgentUpsert = {
    name: draft.name.trim(),
    description: draft.description,
    model: {
      provider: draft.model.provider.trim(),
      model: draft.model.model.trim(),
      // Empty optionals are omitted, not nulled (PATCH hazard).
      ...(draft.model.base_url.trim() !== "" ? { base_url: draft.model.base_url.trim() } : {}),
      ...(credentialRef !== undefined ? { credential_ref: credentialRef } : {}),
    },
    system_prompt: draft.system_prompt,
    ...(draft.user_prompt_template.trim() !== ""
      ? { user_prompt_template: draft.user_prompt_template }
      : {}),
    tools: bindings,
    strategy: {
      type: draft.strategy.type,
      params: (strategyParams.value ?? {}) as Record<string, unknown>,
    },
    memory: {
      enabled: draft.memory.enabled,
      max_messages: draft.memory.max_messages,
      ...(draft.memory.session_key.trim() !== ""
        ? { session_key: draft.memory.session_key }
        : {}),
    },
    max_iterations: draft.max_iterations,
    temperature: draft.temperature,
    ...(outputSchema.value !== undefined
      ? { output_schema: outputSchema.value as Record<string, unknown> }
      : {}),
  };
  return { body };
}

export const useEditorStore = create<EditorState>((set) => ({
  draft: null,
  agentId: null,
  isDirty: false,
  open: (definition, defaults) =>
    set({
      agentId: definition?.id ?? null,
      draft: definition ? draftFromDefinition(definition) : draftForCreate(defaults),
      isDirty: false,
    }),
  close: () => set({ draft: null, agentId: null, isDirty: false }),
  update: (patch) =>
    set((state) =>
      state.draft
        ? { draft: { ...state.draft, ...patch }, isDirty: true }
        : state,
    ),
}));

// Exposed for tests and future reuse (e.g. duplicating an agent).
export { draftFromDefinition, draftForCreate };