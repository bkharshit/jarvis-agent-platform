import { describe, expect, it } from "vitest";

import type { WorkflowDefinition } from "@/api/queries/workflows";
import {
  definitionToGraph,
  graphToPayload,
  hashDefinition,
  stripUnderscoreKeys,
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
          input_template: "{{node.a.output}}",
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
      input_template: "{{node.a.output}}",
    });
    expect(Object.keys(nodeB.config).some((k) => k.startsWith("_"))).toBe(false);
  });
});