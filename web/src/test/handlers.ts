import { http, HttpResponse } from "msw";

// Default handlers for the suite — happy-path stand-ins keyed to the real
// API surface. Per-test cases use server.use(...) on top of these.

/** A realistic AgentDefinition matching the backend schema. */
export const agentFixture = {
  id: "agent-1",
  name: "Research agent",
  description: "Finds things",
  model: { provider: "mock", model: "mock-agent" },
  system_prompt: "You research.",
  tools: [{ name: "calculator", enabled: true, config: {} }],
  strategy: { type: "function_calling" as const, params: {} },
  memory: { enabled: false, max_messages: 20 },
  max_iterations: 8,
  temperature: 0.7,
  created_at: "2026-09-04T12:00:00Z",
  updated_at: "2026-09-04T12:00:00Z",
};

export const agentsFixture = [agentFixture];

export const handlers = [
  http.get("/v1/agents", () => HttpResponse.json({ items: agentsFixture })),
  http.post("/v1/agents", () => HttpResponse.json({ definition: agentFixture, versions: [] }, { status: 201 })),
  http.get("/v1/agents/:agent_id", ({ params }) => {
    const { agent_id } = params as { agent_id: string };
    if (agent_id === agentFixture.id) {
      return HttpResponse.json({ definition: agentFixture, versions: [] });
    }
    return HttpResponse.json(
      { error: { kind: "not_found", message: `agent ${agent_id} not found` } },
      { status: 404 },
    );
  }),
  http.patch("/v1/agents/:agent_id", () =>
    HttpResponse.json({ definition: agentFixture, versions: [] }),
  ),
  http.delete("/v1/agents/:agent_id", () => new HttpResponse(null, { status: 204 })),
  http.get("/v1/capabilities", () => {
    return HttpResponse.json({
      sections: {
        agents: { enabled: true, summary: "Create, version, and run agents" },
        executions: { enabled: true, summary: "Browse runs" },
        conversations: { enabled: true, summary: "Per-session history" },
        tools: { enabled: true, summary: "Builtin registry" },
        models: { enabled: true, mode: "read-only", summary: "Provider info" },
        workflows: {
          enabled: false,
          stage: "S6",
          summary: "DAG runs reusing the same event model",
        },
        knowledge: { enabled: false, stage: "S8", summary: "Datasets and retrieval" },
        evaluations: { enabled: false, stage: "S11", summary: "Datasets, runs, and scores" },
        observability: { enabled: false, stage: "S7", summary: "Traces and spans" },
        plugins: { enabled: false, stage: "S3", summary: "Strategy plugins" },
        triggers: { enabled: false, stage: "S13", summary: "Cron, webhook, and event rules" },
        settings: { enabled: false, stage: "S2", summary: "Auth, tenants, API keys" },
      },
    });
  }),
];

/** Build a frozen-shape error envelope body (D13/D16). */
export function envelope(
  kind: string,
  message: string,
  details?: unknown,
): { error: { kind: string; message: string; details?: unknown } } {
  return { error: { kind, message, ...(details !== undefined ? { details } : {}) } };
}