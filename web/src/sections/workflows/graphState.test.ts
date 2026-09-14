import { describe, expect, it } from "vitest";

import type { WorkflowDefinition } from "@/api/queries/workflows";
import {
  definitionToGraph,
  graphToPayload,
  hashDefinition,
  nodeIdError,
  renameNodeId,
  stripUnderscoreKeys,
  type CanvasGraph,
} from "./graphState";

describe("stripUnderscoreKeys", () => {
  it("removes _-prefixed keys at every depth (the save-time strip)", () => {
    expect(
      stripUnderscoreKeys({
        _label: "Research",
        type: "agent",
        config: {
          agent_id: "a1",
          _ui: { collapsed: true },
          input_template: "{{input}}",
        },
      }),
    ).toEqual({
      type: "agent",
      config: { agent_id: "a1", input_template: "{{input}}" },
    });
  });

  it("leaves plain values and arrays untouched", () => {
    expect(stripUnderscoreKeys([1, { a: 1 }, "x"])).toEqual([1, { a: 1 }, "x"]);
    expect(stripUnderscoreKeys("plain")).toBe("plain");
  });
});

describe("hashDefinition", () => {
  it("is stable across key order (the server-hash conflict guard)", () => {
    expect(hashDefinition({ a: 1, b: 2 })).toBe(hashDefinition({ b: 2, a: 1 }));
    expect(hashDefinition({ a: 1 })).not.toBe(hashDefinition({ a: 2 }));
  });
});

describe("renameNodeId", () => {
  const graph: CanvasGraph = {
    nodes: [
      {
        id: "a",
        type: "workflowNode",
        position: { x: 0, y: 0 },
        data: { type: "agent", config: { agent_id: "x1", input_template: "{{input}}" } },
      },
      {
        id: "agent-2",
        type: "workflowNode",
        position: { x: 1, y: 0 },
        data: {
          type: "agent",
          config: {
            agent_id: "x2",
            input_template: "digest of {{node.a}} and my own {{node.agent-2}}",
          },
        },
      },
      {
        id: "c",
        type: "workflowNode",
        position: { x: 2, y: 0 },
        data: {
          type: "condition",
          config: {
            routes: [{ when: { operator: "contains", value: "x" }, to_node: "agent-2" }],
            else_node: "a",
          },
        },
      },
    ],
    edges: [
      { id: "a->agent-2", source: "a", target: "agent-2" },
      { id: "agent-2->c", source: "agent-2", target: "c" },
    ],
    startNodeId: "a",
  };

  it("renames the node and every reference to it", () => {
    const next = renameNodeId(graph, "agent-2", "reader");
    expect(next.nodes.map((n) => n.id)).toEqual(["a", "reader", "c"]);
    expect(next.edges).toEqual([
      { id: "a->reader", source: "a", target: "reader" },
      { id: "reader->c", source: "reader", target: "c" },
    ]);
    expect(next.startNodeId).toBe("a");
    // template refs in OTHER nodes' configs follow the rename
    expect(next.nodes[1].data.config.input_template).toBe(
      "digest of {{node.a}} and my own {{node.reader}}",
    );
    // condition structural targets follow
    const condition = next.nodes[2].data.config as {
      routes: { to_node: string }[];
      else_node: string;
    };
    expect(condition.routes[0].to_node).toBe("reader");
    expect(condition.else_node).toBe("a");
  });

  it("renames a dotted hop but not a longer id sharing the prefix", () => {
    const g: CanvasGraph = {
      nodes: [
        { id: "a", type: "workflowNode", position: { x: 0, y: 0 }, data: { type: "tool", config: {} } },
        {
          id: "b",
          type: "workflowNode",
          position: { x: 1, y: 0 },
          data: {
            type: "agent",
            config: { input_template: "{{node.a}} {{node.a2}} {{node.a.field}}" },
          },
        },
      ],
      edges: [],
      startNodeId: "a",
    };
    const next = renameNodeId(g, "a", "z");
    expect(next.nodes[1].data.config.input_template).toBe(
      "{{node.z}} {{node.a2}} {{node.z.field}}",
    );
  });

  it("keeps the start node pointing at the renamed node", () => {
    const next = renameNodeId(graph, "a", "start");
    expect(next.startNodeId).toBe("start");
    expect(next.nodes[0].id).toBe("start");
  });
});

describe("nodeIdError", () => {
  it("enforces the backend's node-id pattern and uniqueness", () => {
    expect(nodeIdError("", ["a"])).toBe("id must not be empty");
    expect(nodeIdError("bad id", ["a"])).toContain("letters, digits");
    expect(nodeIdError("a.b", ["a"])).toContain("letters, digits");
    expect(nodeIdError("a", ["a", "b"])).toContain("another node");
    expect(nodeIdError("a", ["a"], "a")).toBe(null); // renaming to itself
    expect(nodeIdError("agent-1_x", ["b"])).toBe(null); // hyphen/underscore fine
  });
});

describe("definition ⇄ canvas ⇄ payload round-trip", () => {
  const definition = {
    id: "wf-1",
    name: "chain",
    description: "",
    nodes: [
      {
        id: "a",
        type: "agent",
        config: {
          agent_id: "agent-1",
          agent_version_id: "ver-9",
          input_template: "{{input}}",
        },
      },
      {
        id: "b",
        type: "agent",
        config: {
          agent_id: "agent-2",
          agent_version_id: null,
          input_template: "{{node.a}}",
        },
      },
    ],
    edges: [{ from_node: "a", to_node: "b" }],
    start_node_id: "a",
    max_node_executions: 24,
    created_at: "2026-09-14T00:00:00Z",
    updated_at: "2026-09-14T00:00:00Z",
  } as unknown as WorkflowDefinition;

  it("lays nodes out by BFS depth and preserves the graph on save", () => {
    const graph = definitionToGraph(definition);
    expect(graph.nodes.map((n) => n.id)).toEqual(["a", "b"]);
    expect(graph.edges).toEqual([{ id: "a->b", source: "a", target: "b" }]);
    expect(graph.startNodeId).toBe("a");
    // pinned version survives the canvas (D42 shows, next publish re-pins)
    const payload = graphToPayload(graph, {
      name: "chain",
      description: "",
      maxNodeExecutions: 24,
    });
    expect(payload.nodes[0]).toEqual({
      id: "a",
      type: "agent",
      config: { agent_id: "agent-1", agent_version_id: "ver-9", input_template: "{{input}}" },
    });
    expect(payload.edges).toEqual([{ from_node: "a", to_node: "b" }]);
    expect(payload.start_node_id).toBe("a");
  });

  it("strips UI-only keys from node configs at save", () => {
    const graph = definitionToGraph(definition);
    graph.nodes[1].data = { type: "agent", config: { ...graph.nodes[1].data.config, _label: "hi" } };
    const payload = graphToPayload(graph, {
      name: "chain",
      description: "",
      maxNodeExecutions: 24,
    });
    const nodeB = payload.nodes[1] as { config: Record<string, unknown> };
    expect(nodeB.config).toEqual({
      agent_id: "agent-2",
      agent_version_id: null,
      input_template: "{{node.a}}",
    });
    expect(Object.keys(nodeB.config).some((k) => k.startsWith("_"))).toBe(false);
  });
});