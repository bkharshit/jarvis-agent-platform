import type {
  WorkflowDefinition,
  WorkflowEdge,
  WorkflowNode,
  WorkflowUpsert,
} from "@/api/queries/workflows";

// Canvas state ↔ API payload (S6). The canvas node's data may carry
// UI-only keys prefixed with `_` — `stripUnderscoreKeys` removes them
// recursively at save, so the server's extra="forbid" payload never sees
// them. Everything else round-trips through the typed schema.

export type NodeType = "agent" | "tool" | "condition";

/** UI-only canvas data: `config` is the typed node config (as JSON), `_`
 * keys are display-only and stripped on save. */
export interface CanvasNodeData {
  type: NodeType;
  config: Record<string, unknown>;
  [key: string]: unknown;
}

export interface CanvasNode {
  id: string;
  type: "workflowNode";
  position: { x: number; y: number };
  data: CanvasNodeData;
}

export interface CanvasEdge {
  id: string;
  source: string;
  target: string;
}

export interface CanvasGraph {
  nodes: CanvasNode[];
  edges: CanvasEdge[];
  startNodeId: string;
}

/** Recursively drop keys starting with `_` (UI-only fields). */
export function stripUnderscoreKeys(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(stripUnderscoreKeys);
  if (value !== null && typeof value === "object") {
    const out: Record<string, unknown> = {};
    for (const [key, entry] of Object.entries(value as Record<string, unknown>)) {
      if (key.startsWith("_")) continue;
      out[key] = stripUnderscoreKeys(entry);
    }
    return out;
  }
  return value;
}

/** The UpsertRequest payload: name/graph + strip. */
export function graphToPayload(
  graph: CanvasGraph,
  meta: { name: string; description: string; maxNodeExecutions: number },
): WorkflowUpsert {
  const nodes = graph.nodes.map((node) => ({
    id: node.id,
    type: node.data.type,
    config: stripUnderscoreKeys(node.data.config),
  }));
  return {
    name: meta.name,
    description: meta.description,
    nodes: nodes as WorkflowUpsert["nodes"],
    edges: graph.edges.map((edge) => ({ from_node: edge.source, to_node: edge.target })),
    start_node_id: graph.startNodeId,
    max_node_executions: meta.maxNodeExecutions,
  };
}

/** Layout: BFS depth → column, index within depth → row. The server
 * definition carries no positions — the canvas derives its own. */
export function definitionToGraph(definition: WorkflowDefinition): CanvasGraph {
  const edges = definition.edges ?? [];
  const outgoing = new Map<string, string[]>();
  for (const edge of edges) {
    const list = outgoing.get(edge.from_node) ?? [];
    list.push(edge.to_node);
    outgoing.set(edge.from_node, list);
  }
  const depth = new Map<string, number>([[definition.start_node_id, 0]]);
  const frontier = [definition.start_node_id];
  while (frontier.length > 0) {
    const current = frontier.shift()!;
    for (const next of outgoing.get(current) ?? []) {
      if (!depth.has(next)) {
        depth.set(next, (depth.get(current) ?? 0) + 1);
        frontier.push(next);
      }
    }
  }
  const perColumn = new Map<number, number>();
  const nodes = definition.nodes.map((node) => {
    const level = depth.get(node.id) ?? 0;
    const row = perColumn.get(level) ?? 0;
    perColumn.set(level, row + 1);
    return canvasNodeFromDefinition(node, level, row);
  });
  return {
    nodes,
    edges: edges.map((edge: WorkflowEdge) => ({
      id: `${edge.from_node}->${edge.to_node}`,
      source: edge.from_node,
      target: edge.to_node,
    })),
    startNodeId: definition.start_node_id,
  };
}

function canvasNodeFromDefinition(
  node: WorkflowNode,
  level: number,
  row: number,
): CanvasNode {
  return {
    id: node.id,
    type: "workflowNode",
    position: { x: level * 260, y: row * 150 },
    data: {
      type: node.type,
      // The saved pin survives the round-trip; the editor shows it but the
      // next publish re-pins (D42).
      config: node.config as unknown as Record<string, unknown>,
    },
  };
}

/** The schema's WorkflowNode is a closed union — the editor keeps the same
 * shape while typing. */
export function canvasNodeToDefinitionNode(node: CanvasNode): WorkflowNode {
  return {
    id: node.id,
    type: node.data.type,
    config: stripUnderscoreKeys(node.data.config) as WorkflowNode["config"],
  } as WorkflowNode;
}

/** Stable, dependency-free hash (FNV-1a over the JSON) — the editor's
 * server-hash conflict guard. */
export function hashDefinition(value: unknown): string {
  const json = JSON.stringify(value, ObjectKeysSortReplacer);
  let hash = 0x811c9dc5;
  for (let i = 0; i < json.length; i++) {
    hash ^= json.charCodeAt(i);
    hash = Math.imul(hash, 0x01000193) >>> 0;
  }
  return hash.toString(16);
}

/** JSON.stringify replacer that sorts object keys — hash stability across
 * refetches that reorder. */
const ObjectKeysSortReplacer = (_key: string, value: unknown): unknown => {
  if (value !== null && typeof value === "object" && !Array.isArray(value)) {
    const sorted: Record<string, unknown> = {};
    for (const k of Object.keys(value as Record<string, unknown>).sort()) {
      sorted[k] = (value as Record<string, unknown>)[k];
    }
    return sorted;
  }
  return value;
};

// --- node id rename ---------------------------------------------------------

/** The backend's node-id rule (WorkflowNode.id pattern) — renames must
 * satisfy it, because `{{node.<id>}}` template refs resolve through it. */
const NODE_ID_PATTERN = /^[a-zA-Z0-9_-]+$/;

/** Why `candidate` cannot be a node id, or null when it can. `currentId`
 * (the node being renamed) is exempt from the uniqueness check. */
export function nodeIdError(
  candidate: string,
  takenIds: string[],
  currentId: string | null = null,
): string | null {
  if (candidate.trim() === "") return "id must not be empty";
  if (!NODE_ID_PATTERN.test(candidate)) return "only letters, digits, '-' and '_'";
  if (takenIds.includes(candidate) && candidate !== currentId) {
    return "another node already uses this id";
  }
  return null;
}

/** Rewrite `{{node.<id>}}` references in a string — including dotted hops
 * (`{{node.<id>.field}}`) — without touching a *longer* id that merely
 * starts with the old one (`{{node.a}}` vs `{{node.a2}}`). */
function renameTemplateRefs(text: string, from: string, to: string): string {
  const escaped = from.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return text.replace(
    new RegExp(`\\{\\{node\\.${escaped}(?=[.}])`, "g"),
    `{{node.${to}`,
  );
}

/** One node config, renamed in place: string values get template refs
 * rewritten; condition route/else targets are structural and compared
 * exactly (they are node ids, not text). */
function renameConfigRefs(
  config: Record<string, unknown>,
  from: string,
  to: string,
): Record<string, unknown> {
  const walk = (value: unknown): unknown => {
    if (typeof value === "string") return renameTemplateRefs(value, from, to);
    if (Array.isArray(value)) return value.map(walk);
    if (value !== null && typeof value === "object") {
      const out: Record<string, unknown> = {};
      for (const [key, entry] of Object.entries(value)) {
        out[key] =
          (key === "to_node" || key === "else_node") && entry === from ? to : walk(entry);
      }
      return out;
    }
    return value;
  };
  return walk(config) as Record<string, unknown>;
}

/** Rename a node id everywhere it can be referenced: the node itself, edge
 * endpoints (and edge ids), start_node_id, condition route targets, and
 * `{{node.<id>}}` templates in every node's config. A rename that missed
 * any of these would silently break the walk at run time. */
export function renameNodeId(graph: CanvasGraph, from: string, to: string): CanvasGraph {
  return {
    nodes: graph.nodes.map((node) => ({
      ...node,
      id: node.id === from ? to : node.id,
      data: { ...node.data, config: renameConfigRefs(node.data.config, from, to) },
    })),
    edges: graph.edges.map((edge) => {
      if (edge.source !== from && edge.target !== from) return edge;
      const source = edge.source === from ? to : edge.source;
      const target = edge.target === from ? to : edge.target;
      return { ...edge, id: `${source}->${target}`, source, target };
    }),
    startNodeId: graph.startNodeId === from ? to : graph.startNodeId,
  };
}