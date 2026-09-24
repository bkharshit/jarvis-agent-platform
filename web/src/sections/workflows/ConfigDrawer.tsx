import { useEffect, useState } from "react";

import type { AgentDefinition, ToolBinding } from "@/api/queries/agents";
import { useAgents, useCreateAgent } from "@/api/queries/agents";
import { useCredentials } from "@/api/queries/settings";
import {
  modelDefaults,
  providerNames,
  strategyNames,
  type BuiltinTool,
} from "@/capabilities/detail";
import { useCapabilities } from "@/capabilities/useCapabilities";
import { Drawer } from "@/components/ui/Drawer";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { Textarea } from "@/components/ui/Textarea";
import { draftForCreate, toSavePayload, type AgentDraft, type CredentialKind } from "@/stores/editorStore";
import { nodeIdError, type CanvasNode } from "./graphState";

// Node config drawer (S6 → UI modernization): the config panel as a
// non-modal slide-over — the canvas stays interactive behind it (real
// browsers hit-test clicks, so a blocking backdrop would swallow palette
// and save actions). Escape closes; clicking away deselects.

const selectClass =
  "w-full cursor-pointer rounded-md border border-neutral-800 bg-neutral-900/70 px-2 py-1.5 text-sm text-neutral-100 transition-colors focus:border-indigo-500 focus:outline-none focus:ring-2 focus:ring-indigo-500/30";

/** Editable node id: the id is the template-reference handle
 * (`{{node.<id>}}`), so a rename rewrites every reference in the graph.
 * Draft commits on blur/Enter; an invalid id surfaces and is not applied. */
function NodeIdField({
  node,
  nodeIds,
  onRename,
}: {
  node: CanvasNode;
  nodeIds: string[];
  onRename: (fromId: string, toId: string) => void;
}) {
  const [draft, setDraft] = useState(node.id);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => setDraft(node.id), [node.id]);
  function commit() {
    const candidate = draft.trim();
    if (candidate === node.id) {
      setError(null);
      return;
    }
    const problem = nodeIdError(candidate, nodeIds, node.id);
    if (problem !== null) {
      setError(problem);
      return;
    }
    onRename(node.id, candidate);
    setError(null);
  }
  return (
    <div className="min-w-0 flex-1">
      <Input
        aria-label="Node id"
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            (e.target as HTMLInputElement).blur();
          }
        }}
        data-testid="node-id-input"
        className="font-mono"
      />
      {error !== null && (
        <p className="mt-1 text-xs text-red-400" role="alert" data-testid="node-id-error">
          {error}
        </p>
      )}
    </div>
  );
}

/** Inline agent creation from the canvas: the same draft shape and payload
 * rules as the full Agents editor (toSavePayload — optionals omitted, never
 * nulled), minus the fields a workflow node doesn't need on the spot. The
 * created agent is selected on the node immediately. */
function QuickAgentCard({ onCreated }: { onCreated: (agentId: string) => void }) {
  const capabilities = useCapabilities();
  const credentials = useCredentials();
  const createAgent = useCreateAgent();
  const [draft, setDraft] = useState<AgentDraft | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (draft === null) setDraft(draftForCreate(modelDefaults(capabilities.data)));
  }, [draft, capabilities.data]);

  if (draft === null) return null;
  const patch = (p: Partial<AgentDraft>) => setDraft({ ...draft, ...p });
  const providers = providerNames(capabilities.data);
  const strategies = strategyNames(capabilities.data);

  function save() {
    const payload = toSavePayload(draft!);
    if (payload.error) {
      setError(payload.error);
      return;
    }
    setError(null);
    createAgent.mutate(payload.body, {
      onSuccess: (detail) => onCreated(detail.definition.id),
      onError: (e) => setError(e instanceof Error ? e.message : String(e)),
    });
  }

  return (
    <div
      className="flex flex-col gap-2 rounded-xl border border-neutral-800/80 bg-neutral-950 p-3"
      data-testid="quick-agent-card"
    >
      <p className="text-xs font-medium text-neutral-300">New agent</p>
      <Input
        aria-label="Agent name"
        placeholder="agent name"
        value={draft.name}
        onChange={(e) => patch({ name: e.target.value })}
        data-testid="quick-agent-name"
      />
      <label className="flex flex-col gap-1 text-xs text-neutral-400">
        Provider
        <select
          value={draft.model.provider}
          onChange={(e) => patch({ model: { ...draft.model, provider: e.target.value } })}
          className={selectClass}
          data-testid="quick-agent-provider"
        >
          {(providers.length > 0 ? providers : [draft.model.provider]).map((p) => (
            <option key={p} value={p}>
              {p}
            </option>
          ))}
        </select>
      </label>
      <label className="flex flex-col gap-1 text-xs text-neutral-400">
        Model
        <Input
          value={draft.model.model}
          onChange={(e) => patch({ model: { ...draft.model, model: e.target.value } })}
          data-testid="quick-agent-model"
        />
      </label>
      <label className="flex flex-col gap-1 text-xs text-neutral-400">
        Base URL (optional)
        <Input
          value={draft.model.base_url}
          placeholder="https://… (OpenAI-compatible)"
          onChange={(e) => patch({ model: { ...draft.model, base_url: e.target.value } })}
          data-testid="quick-agent-base-url"
        />
      </label>
      <label className="flex flex-col gap-1 text-xs text-neutral-400">
        Credential
        <select
          value={draft.model.credential_kind}
          onChange={(e) =>
            patch({
              model: {
                ...draft.model,
                credential_kind: e.target.value as CredentialKind,
                credential_value: "",
              },
            })
          }
          className={selectClass}
          data-testid="quick-agent-credential-kind"
        >
          <option value="">Provider default (no credential ref)</option>
          <option value="env">Environment variable</option>
          <option value="stored">Stored credential (BYOK)</option>
        </select>
      </label>
      {draft.model.credential_kind === "env" && (
        <Input
          aria-label="Environment variable name"
          placeholder="OPENAI_API_KEY"
          value={draft.model.credential_value}
          onChange={(e) => patch({ model: { ...draft.model, credential_value: e.target.value } })}
        />
      )}
      {draft.model.credential_kind === "stored" && (
        <select
          aria-label="Stored credential"
          value={draft.model.credential_value}
          onChange={(e) => patch({ model: { ...draft.model, credential_value: e.target.value } })}
          className={selectClass}
        >
          <option value="">Select a stored credential…</option>
          {(credentials.data ?? []).map((c) => (
            <option key={c.id} value={c.id}>
              {c.name} ({c.provider})
            </option>
          ))}
        </select>
      )}
      <label className="flex flex-col gap-1 text-xs text-neutral-400">
        Strategy
        <select
          value={draft.strategy.type}
          onChange={(e) => patch({ strategy: { ...draft.strategy, type: e.target.value } })}
          className={selectClass}
          data-testid="quick-agent-strategy"
        >
          {(strategies.length > 0 ? strategies : [draft.strategy.type]).map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
      </label>
      <label className="flex flex-col gap-1 text-xs text-neutral-400">
        System prompt
        <Textarea
          rows={3}
          value={draft.system_prompt}
          onChange={(e) => patch({ system_prompt: e.target.value })}
          data-testid="quick-agent-system-prompt"
        />
      </label>
      {error !== null && (
        <p className="text-xs text-red-400" role="alert" data-testid="quick-agent-error">
          {error}
        </p>
      )}
      <div className="flex items-center gap-2">
        <Button
          type="button"
          size="sm"
          data-testid="quick-agent-save"
          onClick={() => void save()}
          disabled={createAgent.isPending}
        >
          {createAgent.isPending ? "Creating…" : "Create agent"}
        </Button>
      </div>
    </div>
  );
}

function AgentForm({
  node,
  onChange,
}: {
  node: CanvasNode;
  onChange: (config: Record<string, unknown>) => void;
}) {
  const { data: agents } = useAgents();
  const [quickOpen, setQuickOpen] = useState(false);
  const config = node.data.config as Record<string, unknown>;
  const agentId = (config.agent_id as string | undefined) ?? "";
  const template = (config.input_template as string | undefined) ?? "{{input}}";
  return (
    <div className="flex flex-col gap-3" data-testid="node-panel-agent">
      <label className="flex flex-col gap-1 text-xs text-neutral-400">
        Agent
        <select
          value={agentId}
          onChange={(e) => onChange({ ...config, agent_id: e.target.value, agent_version_id: null })}
          className={selectClass}
        >
          <option value="">— pick an agent —</option>
          {(agents ?? []).map((a: AgentDefinition) => (
            <option key={a.id} value={a.id}>
              {a.name}
            </option>
          ))}
        </select>
      </label>
      <Button
        variant="ghost"
        size="sm"
        data-testid="new-agent-toggle"
        onClick={() => setQuickOpen((open) => !open)}
        className="self-start"
      >
        {quickOpen ? "close" : "+ new agent"}
      </Button>
      {quickOpen && (
        <QuickAgentCard
          onCreated={(createdId) => {
            setQuickOpen(false);
            onChange({ ...config, agent_id: createdId, agent_version_id: null });
          }}
        />
      )}
      {agentId !== "" && (
        <p className="text-xs text-neutral-500" data-testid="pin-display">
          pins the agent's latest version at publish (D42)
        </p>
      )}
      <label className="flex flex-col gap-1 text-xs text-neutral-400">
        Input template
        <Textarea
          rows={2}
          value={template}
          onChange={(e) => onChange({ ...config, input_template: e.target.value })}
          className="font-mono text-xs"
        />
      </label>
    </div>
  );
}

function ToolForm({
  node,
  builtins,
  onChange,
}: {
  node: CanvasNode;
  builtins: BuiltinTool[];
  onChange: (config: Record<string, unknown>) => void;
}) {
  const config = node.data.config as Record<string, unknown>;
  const binding = (config.binding ?? {}) as ToolBinding;
  const arguments_ = (config.arguments ?? {}) as Record<string, string>;
  function setBinding(name: string) {
    onChange({ ...config, binding: { name: name, config: {} } });
  }
  function setArgument(key: string, value: string) {
    onChange({ ...config, arguments: { ...arguments_, [key]: value } });
  }
  return (
    <div className="flex flex-col gap-3" data-testid="node-panel-tool">
      <label className="flex flex-col gap-1 text-xs text-neutral-400">
        Tool
        <select
          value={binding.name ?? ""}
          onChange={(e) => setBinding(e.target.value)}
          className={selectClass}
        >
          <option value="">— pick a builtin tool —</option>
          {builtins.map((tool) => (
            <option key={tool.name} value={tool.name}>
              {tool.name}
            </option>
          ))}
        </select>
      </label>
      {Object.entries(arguments_).map(([key, value]) => (
        <label key={key} className="flex flex-col gap-1 text-xs text-neutral-400">
          argument {key}
          <Input
            value={value}
            onChange={(e) => setArgument(key, e.target.value)}
            className="font-mono text-xs"
          />
        </label>
      ))}
      <Button
        variant="ghost"
        size="sm"
        data-testid="add-argument"
        onClick={() => setArgument(`arg${Object.keys(arguments_).length + 1}`, "")}
        className="self-start"
      >
        + argument
      </Button>
    </div>
  );
}

function ConditionForm({
  node,
  nodeIds,
  onChange,
}: {
  node: CanvasNode;
  nodeIds: string[];
  onChange: (config: Record<string, unknown>) => void;
}) {
  const config = node.data.config as {
    routes: { when: { operator: string; value: string }; to_node: string }[];
    else_node: string;
  };
  const routes = config.routes ?? [];
  const targetPicker = (value: string, onChangeTarget: (next: string) => void) => (
    <select
      value={value}
      onChange={(e) => onChangeTarget(e.target.value)}
      className={selectClass}
    >
      <option value="">— target —</option>
      {nodeIds.map((id) => (
        <option key={id} value={id}>
          {id}
        </option>
      ))}
    </select>
  );
  return (
    <div className="flex flex-col gap-3" data-testid="node-panel-condition">
      <p className="text-xs text-neutral-500">
        first matching route wins; evaluated against the last upstream output
      </p>
      {routes.map((route, index) => (
        <div
          key={index}
          className="rounded-xl border border-neutral-800/80 bg-neutral-950/60 p-2"
          data-testid="condition-route"
        >
          <select
            value={route.when.operator}
            onChange={(e) => {
              const next = [...routes];
              next[index] = { ...route, when: { ...route.when, operator: e.target.value } };
              onChange({ ...config, routes: next });
            }}
            className={selectClass}
          >
            {["contains", "equals", "regex", "not_empty"].map((op) => (
              <option key={op} value={op}>
                {op}
              </option>
            ))}
          </select>
          <Input
            value={route.when.value}
            onChange={(e) => {
              const next = [...routes];
              next[index] = { ...route, when: { ...route.when, value: e.target.value } };
              onChange({ ...config, routes: next });
            }}
            placeholder="value"
            className="mt-1 font-mono text-xs"
          />
          {targetPicker(route.to_node, (next) => {
            const updated = [...routes];
            updated[index] = { ...route, to_node: next };
            onChange({ ...config, routes: updated });
          })}
        </div>
      ))}
      <Button
        variant="ghost"
        size="sm"
        data-testid="add-route"
        onClick={() => {
          const next = [
            ...routes,
            { when: { operator: "contains", value: "" }, to_node: "" },
          ];
          onChange({ ...config, routes: next });
        }}
        className="self-start"
      >
        + route
      </Button>
      <label className="flex flex-col gap-1 text-xs text-neutral-400">
        else node
        {targetPicker(config.else_node ?? "", (next) => onChange({ ...config, else_node: next }))}
      </label>
    </div>
  );
}

interface ConfigDrawerProps {
  node: CanvasNode | null;
  nodeIds: string[];
  startNodeId: string;
  builtins: BuiltinTool[];
  onChange: (config: Record<string, unknown>) => void;
  onSetStart: (nodeId: string) => void;
  onDelete: (nodeId: string) => void;
  onRename: (fromId: string, toId: string) => void;
  onClose: () => void;
}

/** The selected node's config as a non-modal slide-over. `node-panel` rides
 *  on the body so the per-kind panel testids (`node-panel-agent`, …)
 *  resolve exactly where they did in the inline panel. */
export function ConfigDrawer({
  node,
  nodeIds,
  startNodeId,
  builtins,
  onChange,
  onSetStart,
  onDelete,
  onRename,
  onClose,
}: ConfigDrawerProps) {
  return (
    <Drawer
      open={node !== null}
      onClose={onClose}
      title="Node config"
      modal={false}
      widthClassName="w-[400px]"
    >
      {node !== null && (
        <div className="flex flex-col gap-3" data-testid="node-panel">
          <div className="flex items-start justify-between gap-2">
            <NodeIdField node={node} nodeIds={nodeIds} onRename={onRename} />
            <button
              type="button"
              onClick={() => onDelete(node.id)}
              className="cursor-pointer rounded-lg border border-rose-900 px-2 py-1 text-xs text-rose-300 transition-colors hover:bg-rose-950"
            >
              delete
            </button>
          </div>
          {node.data.type === "agent" && <AgentForm node={node} onChange={onChange} />}
          {node.data.type === "tool" && (
            <ToolForm node={node} builtins={builtins} onChange={onChange} />
          )}
          {node.data.type === "condition" && (
            <ConditionForm node={node} nodeIds={nodeIds} onChange={onChange} />
          )}
          <label className="mt-2 flex items-center gap-2 text-xs text-neutral-400">
            <input
              type="radio"
              checked={startNodeId === node.id}
              onChange={() => onSetStart(node.id)}
              data-testid="set-start"
            />
            start node
          </label>
        </div>
      )}
    </Drawer>
  );
}