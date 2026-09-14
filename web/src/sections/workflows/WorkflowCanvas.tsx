import { useCallback, useEffect, useState } from "react";
import {
  Background,
  Controls,
  Handle,
  Position,
  ReactFlow,
  addEdge,
  applyEdgeChanges,
  applyNodeChanges,
  type Connection,
  type EdgeChange,
  type NodeChange,
  type NodeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";

import type { AgentDefinition, ToolBinding } from "@/api/queries/agents";
import { useAgents, useCreateAgent } from "@/api/queries/agents";
import { useCredentials } from "@/api/queries/settings";
import {
  builtinTools,
  modelDefaults,
  providerNames,
  strategyNames,
  type BuiltinTool,
} from "@/capabilities/detail";
import { useCapabilities } from "@/capabilities/useCapabilities";
import { draftForCreate, toSavePayload, type AgentDraft, type CredentialKind } from "@/stores/editorStore";
import {
  nodeIdError,
  renameNodeId,
  type CanvasEdge,
  type CanvasGraph,
  type CanvasNode,
  type NodeType,
} from "./graphState";

// The workflow canvas (S6): an xyflow graph of the three v1 node types, a
// palette to add nodes, and a config panel for the selected node. The graph
// is a draft in editor state — saving/publishing is the editor's job (D42
// pins happen server-side).

const NODE_TYPE_SUMMARY: Record<NodeType, string> = {
  agent: "runs one agent",
  tool: "executes one tool",
  condition: "routes on the last output",
};

function WorkflowNodeCard({ data, id }: NodeProps) {
  const nodeData = data as { type: NodeType; config: Record<string, unknown> };
  return (
    <div
      className="min-w-40 rounded border border-neutral-700 bg-neutral-900 px-3 py-2 text-xs shadow"
      data-testid={`canvas-node-${nodeData.type}`}
    >
      <Handle type="target" position={Position.Top} />
      <div className="flex items-center justify-between gap-2">
        <span className="font-mono text-sm text-neutral-100">{id}</span>
        <span className="rounded bg-neutral-800 px-1.5 py-0.5 text-[10px] uppercase text-neutral-400">
          {nodeData.type}
        </span>
      </div>
      <p className="mt-1 truncate text-xs text-neutral-500">{NODE_TYPE_SUMMARY[nodeData.type]}</p>
      <Handle type="source" position={Position.Bottom} />
    </div>
  );
}

const nodeTypes = { workflowNode: WorkflowNodeCard } as const;

const NODE_TYPES: NodeType[] = ["agent", "tool", "condition"];

function initialConfig(type: NodeType): Record<string, unknown> {
  if (type === "agent") {
    return { agent_id: "", agent_version_id: null, input_template: "{{input}}" };
  }
  if (type === "tool") return { binding: { name: "", config: {} }, arguments: {} };
  return { routes: [], else_node: "" };
}

// --- config panel ------------------------------------------------------------

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
      <input
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
        className="w-full rounded border border-neutral-700 bg-neutral-900 px-2 py-1 font-mono text-sm text-neutral-100"
      />
      {error !== null && (
        <p className="mt-1 text-xs text-red-400" role="alert" data-testid="node-id-error">
          {error}
        </p>
      )}
    </div>
  );
}

interface PanelProps {
  node: CanvasNode | null;
  nodeIds: string[];
  startNodeId: string;
  builtins: BuiltinTool[];
  onChange: (config: Record<string, unknown>) => void;
  onSetStart: (nodeId: string) => void;
  onDelete: (nodeId: string) => void;
  onRename: (fromId: string, toId: string) => void;
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
  const field =
    "w-full rounded border border-neutral-700 bg-neutral-900 px-2 py-1 text-xs text-neutral-100";

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
      className="flex flex-col gap-2 rounded border border-neutral-800 bg-neutral-950 p-2"
      data-testid="quick-agent-card"
    >
      <p className="text-xs font-medium text-neutral-300">New agent</p>
      <input
        aria-label="Agent name"
        placeholder="agent name"
        value={draft.name}
        onChange={(e) => patch({ name: e.target.value })}
        className={field}
        data-testid="quick-agent-name"
      />
      <label className="flex flex-col gap-1 text-xs text-neutral-400">
        Provider
        <select
          value={draft.model.provider}
          onChange={(e) => patch({ model: { ...draft.model, provider: e.target.value } })}
          className={field}
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
        <input
          value={draft.model.model}
          onChange={(e) => patch({ model: { ...draft.model, model: e.target.value } })}
          className={field}
          data-testid="quick-agent-model"
        />
      </label>
      <label className="flex flex-col gap-1 text-xs text-neutral-400">
        Base URL (optional)
        <input
          value={draft.model.base_url}
          placeholder="https://… (OpenAI-compatible)"
          onChange={(e) => patch({ model: { ...draft.model, base_url: e.target.value } })}
          className={field}
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
          className={field}
          data-testid="quick-agent-credential-kind"
        >
          <option value="">Provider default (no credential ref)</option>
          <option value="env">Environment variable</option>
          <option value="stored">Stored credential (BYOK)</option>
        </select>
      </label>
      {draft.model.credential_kind === "env" && (
        <input
          aria-label="Environment variable name"
          placeholder="OPENAI_API_KEY"
          value={draft.model.credential_value}
          onChange={(e) => patch({ model: { ...draft.model, credential_value: e.target.value } })}
          className={field}
        />
      )}
      {draft.model.credential_kind === "stored" && (
        <select
          aria-label="Stored credential"
          value={draft.model.credential_value}
          onChange={(e) => patch({ model: { ...draft.model, credential_value: e.target.value } })}
          className={field}
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
          className={field}
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
        <textarea
          rows={3}
          value={draft.system_prompt}
          onChange={(e) => patch({ system_prompt: e.target.value })}
          className={field}
          data-testid="quick-agent-system-prompt"
        />
      </label>
      {error !== null && (
        <p className="text-xs text-red-400" role="alert" data-testid="quick-agent-error">
          {error}
        </p>
      )}
      <div className="flex items-center gap-2">
        <button
          type="button"
          data-testid="quick-agent-save"
          onClick={() => void save()}
          disabled={createAgent.isPending}
          className="cursor-pointer rounded bg-neutral-100 px-2 py-1 text-xs font-medium text-neutral-900 hover:bg-white disabled:text-neutral-500"
        >
          {createAgent.isPending ? "Creating…" : "Create agent"}
        </button>
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
          className="rounded border border-neutral-700 bg-neutral-900 px-2 py-1 text-sm text-neutral-100"
        >
          <option value="">— pick an agent —</option>
          {(agents ?? []).map((a: AgentDefinition) => (
            <option key={a.id} value={a.id}>
              {a.name}
            </option>
          ))}
        </select>
      </label>
      <button
        type="button"
        data-testid="new-agent-toggle"
        onClick={() => setQuickOpen((open) => !open)}
        className="cursor-pointer self-start rounded border border-neutral-700 px-2 py-1 text-xs text-neutral-300 hover:bg-neutral-900"
      >
        {quickOpen ? "close" : "+ new agent"}
      </button>
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
        <textarea
          rows={2}
          value={template}
          onChange={(e) => onChange({ ...config, input_template: e.target.value })}
          className="rounded border border-neutral-700 bg-neutral-900 px-2 py-1 font-mono text-xs text-neutral-100"
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
          className="rounded border border-neutral-700 bg-neutral-900 px-2 py-1 text-sm text-neutral-100"
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
          <input
            value={value}
            onChange={(e) => setArgument(key, e.target.value)}
            className="rounded border border-neutral-700 bg-neutral-900 px-2 py-1 font-mono text-xs text-neutral-100"
          />
        </label>
      ))}
      <button
        type="button"
        data-testid="add-argument"
        onClick={() => setArgument(`arg${Object.keys(arguments_).length + 1}`, "")}
        className="cursor-pointer self-start rounded border border-neutral-700 px-2 py-1 text-xs text-neutral-300 hover:bg-neutral-900"
      >
        + argument
      </button>
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
      className="rounded border border-neutral-700 bg-neutral-900 px-2 py-1 text-xs text-neutral-100"
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
        <div key={index} className="rounded border border-neutral-800 p-2" data-testid="condition-route">
          <select
            value={route.when.operator}
            onChange={(e) => {
              const next = [...routes];
              next[index] = { ...route, when: { ...route.when, operator: e.target.value } };
              onChange({ ...config, routes: next });
            }}
            className="rounded border border-neutral-700 bg-neutral-900 px-2 py-1 text-xs text-neutral-100"
          >
            {["contains", "equals", "regex", "not_empty"].map((op) => (
              <option key={op} value={op}>
                {op}
              </option>
            ))}
          </select>
          <input
            value={route.when.value}
            onChange={(e) => {
              const next = [...routes];
              next[index] = { ...route, when: { ...route.when, value: e.target.value } };
              onChange({ ...config, routes: next });
            }}
            placeholder="value"
            className="mt-1 w-full rounded border border-neutral-700 bg-neutral-900 px-2 py-1 font-mono text-xs text-neutral-100"
          />
          {targetPicker(route.to_node, (next) => {
            const updated = [...routes];
            updated[index] = { ...route, to_node: next };
            onChange({ ...config, routes: updated });
          })}
        </div>
      ))}
      <button
        type="button"
        data-testid="add-route"
        onClick={() => {
          const next = [
            ...routes,
            { when: { operator: "contains", value: "" }, to_node: "" },
          ];
          onChange({ ...config, routes: next });
        }}
        className="cursor-pointer self-start rounded border border-neutral-700 px-2 py-1 text-xs text-neutral-300 hover:bg-neutral-900"
      >
        + route
      </button>
      <label className="flex flex-col gap-1 text-xs text-neutral-400">
        else node
        {targetPicker(config.else_node ?? "", (next) => onChange({ ...config, else_node: next }))}
      </label>
    </div>
  );
}

function ConfigPanel({
  node,
  nodeIds,
  startNodeId,
  builtins,
  onChange,
  onSetStart,
  onDelete,
  onRename,
}: PanelProps) {
  if (node === null) {
    return (
      <div className="w-72 shrink-0 text-sm text-neutral-500" data-testid="node-panel-empty">
        Select a node to configure it.
      </div>
    );
  }
  return (
    <div className="w-72 shrink-0 rounded border border-neutral-800 bg-neutral-900/60 p-3" data-testid="node-panel">
      <div className="flex items-start justify-between gap-2">
        <NodeIdField node={node} nodeIds={nodeIds} onRename={onRename} />
        <button
          type="button"
          onClick={() => onDelete(node.id)}
          className="cursor-pointer rounded border border-red-900 px-2 py-0.5 text-xs text-red-300 hover:bg-red-950"
        >
          delete
        </button>
      </div>
      {node.data.type === "agent" && <AgentForm node={node} onChange={onChange} />}
      {node.data.type === "tool" && <ToolForm node={node} builtins={builtins} onChange={onChange} />}
      {node.data.type === "condition" && (
        <ConditionForm node={node} nodeIds={nodeIds} onChange={onChange} />
      )}
      <label className="mt-3 flex items-center gap-2 text-xs text-neutral-400">
        <input
          type="radio"
          checked={startNodeId === node.id}
          onChange={() => onSetStart(node.id)}
          data-testid="set-start"
        />
        start node
      </label>
    </div>
  );
}

// --- canvas ------------------------------------------------------------------

export function WorkflowCanvas({
  graph,
  onChange,
}: {
  graph: CanvasGraph;
  onChange: (graph: CanvasGraph) => void;
}) {
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const { data: capabilities } = useCapabilities();
  const builtins = builtinTools(capabilities);

  const onNodesChange = useCallback(
    (changes: NodeChange<CanvasNode>[]) =>
      onChange({ ...graph, nodes: applyNodeChanges(changes, graph.nodes) }),
    [graph, onChange],
  );
  const onEdgesChange = useCallback(
    (changes: EdgeChange[]) =>
      onChange({ ...graph, edges: applyEdgeChanges(changes, graph.edges) }),
    [graph, onChange],
  );
  const onConnect = useCallback(
    (connection: Connection) =>
      onChange({
        ...graph,
        edges: addEdge(
          { id: `${connection.source}->${connection.target}`, source: connection.source, target: connection.target },
          graph.edges,
        ),
      }),
    [graph, onChange],
  );

  function addNode(type: NodeType) {
    const id = `${type}-${graph.nodes.length + 1}`;
    const node: CanvasNode = {
      id,
      type: "workflowNode",
      position: { x: 40 + graph.nodes.length * 40, y: 40 + graph.nodes.length * 30 },
      data: { type, config: initialConfig(type) },
    };
    onChange({
      ...graph,
      nodes: [...graph.nodes, node],
      startNodeId: graph.nodes.length === 0 ? id : graph.startNodeId,
    });
    setSelectedId(id);
  }

  const selected = graph.nodes.find((n) => n.id === selectedId) ?? null;

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center gap-2" data-testid="node-palette">
        {NODE_TYPES.map((type) => (
          <button
            key={type}
            type="button"
            onClick={() => addNode(type)}
            className="cursor-pointer rounded border border-neutral-700 px-3 py-1.5 text-sm text-neutral-200 hover:bg-neutral-900"
          >
            + {type}
          </button>
        ))}
      </div>
      <div className="flex gap-4">
        <div className="h-96 min-w-0 flex-1 overflow-auto rounded border border-neutral-800">
          <ReactFlow
            nodes={graph.nodes}
            edges={graph.edges}
            nodeTypes={nodeTypes}
            onNodesChange={onNodesChange}
            onEdgesChange={onEdgesChange}
            onConnect={onConnect}
            onNodeClick={(_e, node) => setSelectedId(node.id)}
            onPaneClick={() => setSelectedId(null)}
            fitView
          >
            <Background />
            <Controls />
          </ReactFlow>
        </div>
        <ConfigPanel
          node={selected}
          nodeIds={graph.nodes.map((n) => n.id)}
          startNodeId={graph.startNodeId}
          builtins={builtins}
          onChange={(config) => {
            if (selected === null) return;
            onChange({
              ...graph,
              nodes: graph.nodes.map((n) =>
                n.id === selected.id ? { ...n, data: { ...n.data, config } } : n,
              ),
            });
          }}
          onSetStart={(nodeId) => onChange({ ...graph, startNodeId: nodeId })}
          onRename={(fromId, toId) => {
            onChange(renameNodeId(graph, fromId, toId));
            setSelectedId(toId); // keep the panel on the renamed node
          }}
          onDelete={(nodeId) =>
            onChange({
              ...graph,
              nodes: graph.nodes.filter((n) => n.id !== nodeId),
              edges: graph.edges.filter(
                (e: CanvasEdge) => e.source !== nodeId && e.target !== nodeId,
              ),
              startNodeId:
                graph.startNodeId === nodeId
                  ? (graph.nodes.find((n) => n.id !== nodeId)?.id ?? "")
                  : graph.startNodeId,
            })
          }
        />
      </div>
    </div>
  );
}