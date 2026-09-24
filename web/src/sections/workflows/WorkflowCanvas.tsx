import { useCallback, useState } from "react";
import {
  Background,
  Controls,
  Handle,
  MiniMap,
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

import { builtinTools } from "@/capabilities/detail";
import { useCapabilities } from "@/capabilities/useCapabilities";
import { ConfigDrawer } from "./ConfigDrawer";
import { renameNodeId, type CanvasEdge, type CanvasGraph, type CanvasNode, type NodeType } from "./graphState";

// The workflow canvas (S6): an xyflow graph of the three v1 node types, a
// palette to add nodes, and a config drawer for the selected node. The graph
// is a draft in editor state — saving/publishing is the editor's job (D42
// pins happen server-side).

const NODE_TYPE_SUMMARY: Record<NodeType, string> = {
  agent: "runs one agent",
  tool: "executes one tool",
  condition: "routes on the last output",
};

/** Per-kind visual identity on the canvas: agent = indigo, tool = emerald,
 *  condition = amber (the kind is also the text label — color is never the
 *  only signal). */
const NODE_TYPE_STYLES: Record<NodeType, { bar: string; chip: string }> = {
  agent: {
    bar: "bg-indigo-500",
    chip: "bg-indigo-500/15 text-indigo-300",
  },
  tool: {
    bar: "bg-emerald-500",
    chip: "bg-emerald-500/15 text-emerald-300",
  },
  condition: {
    bar: "bg-amber-500",
    chip: "bg-amber-500/15 text-amber-300",
  },
};

function WorkflowNodeCard({ data, id, selected }: NodeProps) {
  const nodeData = data as { type: NodeType; config: Record<string, unknown> };
  const styles = NODE_TYPE_STYLES[nodeData.type];
  return (
    <div
      className={`min-w-40 overflow-hidden rounded-xl border bg-neutral-900 text-xs shadow-lg transition-shadow ${
        selected
          ? "border-indigo-500/60 ring-2 ring-indigo-500/60"
          : "border-neutral-700/80"
      }`}
      data-testid={`canvas-node-${nodeData.type}`}
    >
      <div className={`h-1 w-full ${styles.bar}`} />
      <div className="px-3 py-2">
        <Handle type="target" position={Position.Top} />
        <div className="flex items-center justify-between gap-2">
          <span className="font-mono text-sm text-neutral-100">{id}</span>
          <span
            className={`rounded px-1.5 py-0.5 text-[10px] uppercase tracking-wide ${styles.chip}`}
          >
            {nodeData.type}
          </span>
        </div>
        <p className="mt-1 truncate text-xs text-neutral-500">
          {NODE_TYPE_SUMMARY[nodeData.type]}
        </p>
        <Handle type="source" position={Position.Bottom} />
      </div>
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
      <div
        className="flex w-fit items-center gap-1.5 rounded-xl border border-neutral-800/80 bg-neutral-900/70 p-1.5 shadow-lg backdrop-blur"
        data-testid="node-palette"
      >
        {NODE_TYPES.map((type) => (
          <button
            key={type}
            type="button"
            onClick={() => addNode(type)}
            className="cursor-pointer rounded-lg px-3 py-1.5 text-sm text-neutral-300 transition-colors hover:bg-neutral-800 hover:text-neutral-100"
          >
            + {type}
          </button>
        ))}
      </div>
      <div className="flex gap-4">
        {/* Node drag is in-session only: the server carries no positions and
            the BFS layout regenerates them on every load. */}
        <div className="h-96 min-w-0 flex-1 overflow-auto rounded-xl border border-neutral-800/80 bg-[#0d0f12]">
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
            <Background gap={20} size={1.2} bgColor="#0d0f12" />
            <Controls showInteractive={false} />
            <MiniMap
              pannable
              zoomable
              nodeColor={(node) => {
                const type = (node.data as { type?: NodeType } | undefined)?.type;
                if (type === "agent") return "#6366f1";
                if (type === "tool") return "#10b981";
                if (type === "condition") return "#f59e0b";
                return "#3f3f46";
              }}
            />
          </ReactFlow>
        </div>
        <ConfigDrawer
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
            setSelectedId(toId); // keep the drawer on the renamed node
          }}
          onDelete={(nodeId) => {
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
            });
            setSelectedId(null);
          }}
          onClose={() => setSelectedId(null)}
        />
      </div>
    </div>
  );
}