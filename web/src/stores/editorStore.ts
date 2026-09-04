import { create } from "zustand";

import type { AgentDefinition, AgentUpsert, ToolBinding } from "@/api/queries/agents";

// Form-shaped draft. Optional API fields are "" here and *omitted* from the
// saved body — the PATCH route does model_dump(exclude_unset=True) +
// model_copy without re-validation, so an explicit null can poison the
// snapshot (plan risk: PATCH null injection). Client-only state never
// reaches the wire (every API schema is extra="forbid").

export interface ToolDraft {
  name: string;
  enabled: boolean;
  config: string; // JSON text; "" = {}
}

export interface AgentDraft {
  name: string;
  description: string;
  model: { provider: string; model: string; base_url: string; api_key_env: string };
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
  open: (definition: AgentDefinition | null) => void;
  close: () => void;
  update: (patch: Partial<AgentDraft>) => void;
}

function draftFromDefinition(definition: AgentDefinition): AgentDraft {
  return {
    name: definition.name,
    description: definition.description,
    model: {
      provider: definition.model.provider,
      model: definition.model.model,
      base_url: definition.model.base_url ?? "",
      api_key_env: definition.model.api_key_env ?? "",
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

function draftForCreate(): AgentDraft {
  return draftFromDefinition({
    id: "",
    name: "",
    description: "",
    model: { provider: "mock", model: "mock-agent" },
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

  const body: AgentUpsert = {
    name: draft.name.trim(),
    description: draft.description,
    model: {
      provider: draft.model.provider.trim(),
      model: draft.model.model.trim(),
      // Empty optional strings are omitted, not nulled (PATCH hazard).
      ...(draft.model.base_url.trim() !== "" ? { base_url: draft.model.base_url.trim() } : {}),
      ...(draft.model.api_key_env.trim() !== ""
        ? { api_key_env: draft.model.api_key_env.trim() }
        : {}),
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
  open: (definition) =>
    set({
      agentId: definition?.id ?? null,
      draft: definition ? draftFromDefinition(definition) : draftForCreate(),
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