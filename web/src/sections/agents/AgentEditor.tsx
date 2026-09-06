import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router";

import { ApiError, fieldErrors } from "@/api/errors";
import { useAgent, useCreateAgent, useUpdateAgent } from "@/api/queries/agents";
import { useModelList } from "@/api/queries/models";
import {
  builtinTools,
  modelDefaults,
  modelProviders,
  providerNames,
  strategyNames,
} from "@/capabilities/detail";
import { useCapabilities } from "@/capabilities/useCapabilities";
import { SectionGate } from "@/capabilities/SectionGate";
import { toSavePayload, useEditorStore } from "@/stores/editorStore";
import type { CredentialKind } from "@/stores/editorStore";
import { toast } from "@/stores/toast";

// Full AgentDefinition editor. Strategy picker and tool bindings come from
// the capabilities payload — never hardcoded (decision 8). Save omits
// undefined AND null (PATCH null-injection hazard).

const inputClass =
  "w-full rounded border border-neutral-700 bg-neutral-900 px-2 py-1.5 text-sm text-neutral-100 focus:border-neutral-500 focus:outline-none";
const labelClass = "block text-xs font-medium text-neutral-400";
const errorClass = "mt-1 text-xs text-red-400";

function FieldError({ message }: { message?: string }) {
  if (!message) return null;
  return <p className={errorClass}>{message}</p>;
}

function AgentEditorForm() {
  const navigate = useNavigate();
  const { agentId } = useParams();
  const { data: detail, isPending, isError, error } = useAgent(agentId);

  const draft = useEditorStore((s) => s.draft);
  const open = useEditorStore((s) => s.open);
  const close = useEditorStore((s) => s.close);
  const update = useEditorStore((s) => s.update);
  const capabilities = useCapabilities();
  const [fieldErrorsMap, setFieldErrorsMap] = useState<Record<string, string>>({});

  useEffect(() => {
    if (agentId === undefined) {
      // creating — seed once; a cached capabilities payload may already be
      // present, and re-running open would wipe in-progress typing.
      if (draft === null) open(null, modelDefaults(capabilities.data));
    } else if (detail && draft === null) {
      open(detail.definition); // editing
    }
  }, [agentId, detail, open, draft, capabilities.data]);

  useEffect(() => close, [close]);

  const createAgent = useCreateAgent();
  const updateAgent = useUpdateAgent(agentId ?? "");
  const saving = createAgent.isPending || updateAgent.isPending;

  // ADR 0007: suggest real model ids from the provider endpoint. Hooks stay
  // unconditional — `draft` is null only while the editor opens.
  const modelList = useModelList(
    draft?.model.provider ?? "",
    draft?.model.base_url ?? "",
    // The env-var name is only meaningful for an env credential ref (S2).
    draft?.model.credential_kind === "env" ? draft.model.credential_value : "",
  );
  const listingError =
    modelList.isError && modelList.error instanceof ApiError
      ? modelList.error.message
      : modelList.isError
        ? "request failed"
        : null;
  const providerDescription = modelProviders(capabilities.data).find(
    (info) => info.name === draft?.model.provider,
  )?.description;

  if (agentId !== undefined && isPending) {
    return <p className="px-6 py-10 text-sm text-neutral-400">Loading agent…</p>;
  }
  if (agentId !== undefined && isError) {
    return (
      <div className="px-6 py-10">
        <p className="text-sm text-red-400">{error.message}</p>
        <button
          type="button"
          onClick={() => navigate("/agents")}
          className="mt-4 cursor-pointer text-sm text-neutral-300 underline"
        >
          Back to agents
        </button>
      </div>
    );
  }
  if (!draft) return <p className="px-6 py-10 text-sm text-neutral-400">Loading editor…</p>;

  const strategies = strategyNames(capabilities.data);
  const tools = builtinTools(capabilities.data);
  const providers = providerNames(capabilities.data);

  function save() {
    const payload = toSavePayload(draft!);
    if (payload.error) {
      toast("error", payload.error);
      return;
    }
    const onError = (err: unknown) => {
      if (err instanceof ApiError && err.status === 422) {
        setFieldErrorsMap(fieldErrors(err));
      } else if (err instanceof ApiError) {
        // 409 and friends: the server message is user-relevant — verbatim.
        toast("error", err.message);
      } else {
        toast("error", "save failed");
      }
    };
    if (agentId === undefined) {
      createAgent.mutate(payload.body, {
        onSuccess: () => navigate("/agents"),
        onError,
      });
    } else {
      updateAgent.mutate(payload.body, {
        onSuccess: () => navigate("/agents"),
        onError,
      });
    }
  }

  return (
    <div className="mx-auto max-w-3xl px-6 py-8">
      <h1 className="text-xl font-semibold">
        {agentId === undefined ? "New agent" : `Edit: ${draft.name || "agent"}`}
      </h1>

      <form
        className="mt-6 flex flex-col gap-5"
        onSubmit={(e) => {
          e.preventDefault();
          save();
        }}
      >
        <div>
          <label htmlFor="agent-name" className={labelClass}>Name</label>
          <input
            id="agent-name"
            className={inputClass}
            value={draft.name}
            onChange={(e) => update({ name: e.target.value })}
          />
          <FieldError message={fieldErrorsMap.name} />
        </div>

        <div>
          <label htmlFor="agent-description" className={labelClass}>Description</label>
          <input
            id="agent-description"
            className={inputClass}
            value={draft.description}
            onChange={(e) => update({ description: e.target.value })}
          />
          <FieldError message={fieldErrorsMap.description} />
        </div>

        <fieldset className="rounded border border-neutral-800 p-4">
          <legend className="px-1 text-xs font-medium text-neutral-400">Model</legend>
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label htmlFor="model-provider" className={labelClass}>Provider</label>
              <select
                id="model-provider"
                className={inputClass}
                value={draft.model.provider}
                onChange={(e) => update({ model: { ...draft.model, provider: e.target.value } })}
              >
                {(providers.length > 0 ? providers : [draft.model.provider]).map((p) => (
                  <option
                    key={p}
                    value={p}
                    title={modelProviders(capabilities.data).find((info) => info.name === p)?.description}
                  >
                    {p}
                  </option>
                ))}
              </select>
              {providerDescription && (
                <p className="mt-1 text-xs text-neutral-500">{providerDescription}</p>
              )}
              <FieldError message={fieldErrorsMap.provider} />
            </div>
            <div>
              <label htmlFor="model-name" className={labelClass}>Model</label>
              <input
                id="model-name"
                className={inputClass}
                value={draft.model.model}
                list="model-options"
                onChange={(e) => update({ model: { ...draft.model, model: e.target.value } })}
              />
              {/* ADR 0007: real ids the endpoint answered with; free text
                  stays valid — a failed/absent listing never blocks. */}
              <datalist id="model-options">
                {(modelList.data?.models ?? []).map((m) => (
                  <option key={m} value={m} />
                ))}
              </datalist>
              {listingError && (
                <p className="mt-1 text-xs text-amber-400">
                  Model listing unavailable ({listingError}) — type a model id manually.
                </p>
              )}
              <FieldError message={fieldErrorsMap.model} />
            </div>
            <div>
              <label htmlFor="model-base-url" className={labelClass}>Base URL (optional)</label>
              <input
                id="model-base-url"
                className={inputClass}
                value={draft.model.base_url}
                placeholder="https://… (OpenAI-compatible endpoints)"
                onChange={(e) => update({ model: { ...draft.model, base_url: e.target.value } })}
              />
            </div>
            <div>
              <label htmlFor="model-credential-kind" className={labelClass}>Credential (optional)</label>
              <select
                id="model-credential-kind"
                className={inputClass}
                value={draft.model.credential_kind}
                onChange={(e) =>
                  update({
                    model: {
                      ...draft.model,
                      credential_kind: e.target.value as CredentialKind,
                      credential_value: "",
                    },
                  })
                }
              >
                <option value="">Provider default (no credential ref)</option>
                <option value="env">Environment variable</option>
                <option value="stored">Stored credential (BYOK)</option>
              </select>
              {draft.model.credential_kind !== "" && (
                <input
                  id="model-credential-value"
                  className={`${inputClass} mt-1`}
                  value={draft.model.credential_value}
                  placeholder={
                    draft.model.credential_kind === "env"
                      ? "OPENAI_API_KEY"
                      : "credential id (Settings → Credentials)"
                  }
                  onChange={(e) =>
                    update({ model: { ...draft.model, credential_value: e.target.value } })
                  }
                />
              )}
              <p className="mt-1 text-xs text-neutral-500">
                {draft.model.credential_kind === "stored"
                  ? "References a BYOK credential owned by your tenant — the secret itself never enters the definition (ADR 0006)."
                  : "Names an environment variable — the key itself is never stored."}
              </p>
            </div>
          </div>
        </fieldset>

        <div>
          <label htmlFor="agent-system-prompt" className={labelClass}>System prompt</label>
          <textarea
            id="agent-system-prompt"
            rows={4}
            className={inputClass}
            value={draft.system_prompt}
            onChange={(e) => update({ system_prompt: e.target.value })}
          />
        </div>

        <div>
          <label htmlFor="agent-user-template" className={labelClass}>
            User prompt template (optional; {"{{input}}"} placeholder)
          </label>
          <input
            id="agent-user-template"
            className={inputClass}
            value={draft.user_prompt_template}
            onChange={(e) => update({ user_prompt_template: e.target.value })}
          />
        </div>

        <div>
          <label htmlFor="agent-strategy" className={labelClass}>Strategy</label>
          <select
            id="agent-strategy"
            className={inputClass}
            value={draft.strategy.type}
            onChange={(e) =>
              update({
                strategy: {
                  type: e.target.value as typeof draft.strategy.type,
                  params: draft.strategy.params,
                },
              })
            }
          >
            {(strategies.length > 0 ? strategies : [draft.strategy.type]).map((s) => (
              <option key={s} value={s}>{s}</option>
            ))}
          </select>
          <label htmlFor="agent-strategy-params" className={`${labelClass} mt-2`}>
            Strategy params (JSON)
          </label>
          <textarea
            id="agent-strategy-params"
            rows={2}
            className={`${inputClass} font-mono`}
            value={draft.strategy.params}
            onChange={(e) =>
              update({
                strategy: { type: draft.strategy.type, params: e.target.value },
              })
            }
          />
        </div>

        <fieldset className="rounded border border-neutral-800 p-4">
          <legend className="px-1 text-xs font-medium text-neutral-400">Tools</legend>
          {draft.tools.length === 0 && (
            <p className="text-sm text-neutral-500">No tools bound.</p>
          )}
          <ul className="flex flex-col gap-2">
            {draft.tools.map((tool, index) => (
              <li key={tool.name} className="flex items-start gap-3">
                <input
                  type="checkbox"
                  aria-label={`Enable ${tool.name}`}
                  checked={tool.enabled}
                  onChange={(e) =>
                    update({
                      tools: draft.tools.map((t, i) =>
                        i === index ? { ...t, enabled: e.target.checked } : t,
                      ),
                    })
                  }
                />
                <div className="min-w-0 flex-1">
                  <span className="text-sm text-neutral-100">{tool.name}</span>
                  <textarea
                    aria-label={`${tool.name} config (JSON)`}
                    rows={2}
                    className={`${inputClass} mt-1 font-mono`}
                    value={tool.config}
                    onChange={(e) =>
                      update({
                        tools: draft.tools.map((t, i) =>
                          i === index ? { ...t, config: e.target.value } : t,
                        ),
                      })
                    }
                  />
                </div>
                <button
                  type="button"
                  aria-label={`Remove ${tool.name}`}
                  onClick={() =>
                    update({ tools: draft.tools.filter((_, i) => i !== index) })
                  }
                  className="cursor-pointer text-xs text-red-400 hover:text-red-300"
                >
                  Remove
                </button>
              </li>
            ))}
          </ul>
          <select
            aria-label="Add tool"
            className={`${inputClass} mt-3`}
            value=""
            onChange={(e) => {
              if (e.target.value === "") return;
              if (draft.tools.some((t) => t.name === e.target.value)) return;
              update({ tools: [...draft.tools, { name: e.target.value, enabled: true, config: "" }] });
            }}
          >
            <option value="">Add tool…</option>
            {tools
              .filter((t) => !draft.tools.some((bound) => bound.name === t.name))
              .map((t) => (
                <option key={t.name} value={t.name}>{t.name}</option>
              ))}
          </select>
        </fieldset>

        <fieldset className="rounded border border-neutral-800 p-4">
          <legend className="px-1 text-xs font-medium text-neutral-400">Memory</legend>
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={draft.memory.enabled}
              onChange={(e) =>
                update({ memory: { ...draft.memory, enabled: e.target.checked } })
              }
            />
            Enabled
          </label>
          <div className="mt-2 grid grid-cols-2 gap-4">
            <div>
              <label htmlFor="memory-max" className={labelClass}>Max messages</label>
              <input
                id="memory-max"
                type="number"
                min={1}
                max={200}
                className={inputClass}
                value={draft.memory.max_messages}
                onChange={(e) =>
                  update({
                    memory: { ...draft.memory, max_messages: Number(e.target.value) },
                  })
                }
              />
            </div>
            <div>
              <label htmlFor="memory-session-key" className={labelClass}>Session key (optional)</label>
              <input
                id="memory-session-key"
                className={inputClass}
                value={draft.memory.session_key}
                onChange={(e) =>
                  update({
                    memory: { ...draft.memory, session_key: e.target.value },
                  })
                }
              />
            </div>
          </div>
        </fieldset>

        <div className="grid grid-cols-2 gap-4">
          <div>
            <label htmlFor="agent-max-iterations" className={labelClass}>Max iterations (1–32)</label>
            <input
              id="agent-max-iterations"
              type="number"
              min={1}
              max={32}
              className={inputClass}
              value={draft.max_iterations}
              onChange={(e) => update({ max_iterations: Number(e.target.value) })}
            />
            <FieldError message={fieldErrorsMap.max_iterations} />
          </div>
          <div>
            <label htmlFor="agent-temperature" className={labelClass}>Temperature (0–2)</label>
            <input
              id="agent-temperature"
              type="number"
              min={0}
              max={2}
              step={0.1}
              className={inputClass}
              value={draft.temperature}
              onChange={(e) => update({ temperature: Number(e.target.value) })}
            />
            <FieldError message={fieldErrorsMap.temperature} />
          </div>
        </div>

        <div>
          <label htmlFor="agent-output-schema" className={labelClass}>
            Output schema (JSON, optional)
          </label>
          <textarea
            id="agent-output-schema"
            rows={3}
            className={`${inputClass} font-mono`}
            value={draft.output_schema}
            onChange={(e) => update({ output_schema: e.target.value })}
          />
        </div>

        <div className="flex gap-3">
          <button
            type="submit"
            disabled={saving}
            className="cursor-pointer rounded bg-neutral-100 px-4 py-1.5 text-sm font-medium text-neutral-900 hover:bg-white disabled:cursor-not-allowed disabled:text-neutral-500"
          >
            {saving ? "Saving…" : "Save"}
          </button>
          <button
            type="button"
            onClick={() => navigate("/agents")}
            className="cursor-pointer rounded border border-neutral-700 px-4 py-1.5 text-sm text-neutral-300 hover:bg-neutral-900"
          >
            Cancel
          </button>
        </div>
      </form>
    </div>
  );
}

export function AgentEditor() {
  return (
    <SectionGate sectionKey="agents">
      <AgentEditorForm />
    </SectionGate>
  );
}