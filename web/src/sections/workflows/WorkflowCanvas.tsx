import { useCallback, useState } from "react";
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
import { useAgents } from "@/api/queries/agents";
import { builtinTools, type BuiltinTool } from "@/capabilities/detail";
import { useCapabilities } from "@/capabilities/useCapabilities";
import type { CanvasEdge, CanvasGraph, CanvasNode, NodeType } from "./graphState";

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

interface PanelProps {
  node: CanvasNode | null;
  nodeIds: string[];
  startNodeId: string;
  builtins: BuiltinTool[];
  onChange: (config: Record<string, unknown>) => void;
  onSetStart: (nodeId: string) => void;
  onDelete: (nodeId: string) => void;
}

function AgentForm({
  node,
  onChange,
}: {
  node: CanvasNode;
  onChange: (config: Record<string, unknown>) => void;
}) {
  const { data: agents } = useAgents();
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

function ConfigPanel({ node, nodeIds, startNodeId, builtins, onChange, onSetStart, onDelete }: PanelProps) {
  if (node === null) {
    return (
      <div className="w-72 shrink-0 text-sm text-neutral-500" data-testid="node-panel-empty">
        Select a node to configure it.
      </div>
    );
  }
  return (
    <div className="w-72 shrink-0 rounded border border-neutral-800 bg-neutral-900/60 p-3" data-testid="node-panel">
      <div className="flex items-center justify-between">
        <span className="font-mono text-sm text-neutral-100">{node.id}</span>
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