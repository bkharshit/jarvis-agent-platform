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

export const versionSummaryFixture = {
  version: 1,
  id: "ver-1",
  label: "initial",
  created_at: "2026-09-04T12:00:00Z",
};

/** A realistic RunResult row. */
export const runFixture = {
  run_id: "run-abc-123",
  agent_id: "agent-1",
  status: "succeeded" as const,
  input: "what is 2+2?",
  agent_version_id: "ver-1",
  session_id: "session-7",
  trace_id: "",
  final_message: "It is 4.",
  total_usage: { input_tokens: 12, output_tokens: 4, extra: {} },
  iterations: 1,
  error: null,
  error_kind: null,
  started_at: "2026-09-04T13:00:00Z",
  finished_at: "2026-09-04T13:00:02Z",
  event_cursor: 6,
};

export const executionDetailFixture = {
  run: runFixture,
  messages: [
    {
      role: "user",
      content: [{ type: "text", text: "what is 2+2?" }],
    },
    {
      role: "assistant",
      content: [{ type: "text", text: "It is 4." }],
    },
  ],
  tool_executions: [
    {
      tool_call_id: "call-1",
      tool_name: "calculator",
      output: "4",
      is_error: false,
      latency_ms: 3,
    },
  ],
};

/** Cursor-tagged event log matching runFixture (replay fixture). */
export const eventLogFixture = [
  { cursor: 0, event: { event_id: "r0", run_id: "run-abc-123", type: "run.started", agent_id: "agent-1", agent_version_id: "ver-1", session_id: "session-7", input: "what is 2+2?" } },
  { cursor: 1, event: { event_id: "r1", run_id: "run-abc-123", type: "iteration.started", iteration: 1 } },
  { cursor: 2, event: { event_id: "r2", run_id: "run-abc-123", type: "tool.call.requested", tool_call_id: "call-1", name: "calculator", arguments: { expression: "2+2" } } },
  { cursor: 3, event: { event_id: "r3", run_id: "run-abc-123", type: "tool.call.completed", tool_call_id: "call-1", name: "calculator", output: "4", is_error: false, latency_ms: 3 } },
  { cursor: 4, event: { event_id: "r4", run_id: "run-abc-123", type: "text.delta", text: "It is 4." } },
  { cursor: 5, event: { event_id: "r5", run_id: "run-abc-123", type: "run.completed", final_message: "It is 4.", total_usage: { input_tokens: 12, output_tokens: 4, extra: {} }, iterations: 1 } },
];

export const handlers = [
  http.get("/v1/agents", () => HttpResponse.json({ items: agentsFixture })),
  http.post("/v1/agents", () => HttpResponse.json({ definition: agentFixture, versions: [] }, { status: 201 })),
  http.get("/v1/agents/:agent_id", ({ params }) => {
    const { agent_id } = params as { agent_id: string };
    if (agent_id === agentFixture.id) {
      return HttpResponse.json({ definition: agentFixture, versions: [versionSummaryFixture] });
    }
    return HttpResponse.json(
      { error: { kind: "not_found", message: `agent ${agent_id} not found` } },
      { status: 404 },
    );
  }),
  http.get("/v1/agents/:agent_id/versions/:version", ({ params }) => {
    const { version } = params as { version: string };
    const n = Number(version);
    if (n === versionSummaryFixture.version) {
      return HttpResponse.json({
        id: "ver-1",
        agent_id: agentFixture.id,
        version: n,
        snapshot: agentFixture,
        label: versionSummaryFixture.label,
        created_at: versionSummaryFixture.created_at,
      });
    }
    return HttpResponse.json(
      { error: { kind: "not_found", message: `version ${version} not found` } },
      { status: 404 },
    );
  }),
  http.patch("/v1/agents/:agent_id", () =>
    HttpResponse.json({ definition: agentFixture, versions: [] }),
  ),
  http.delete("/v1/agents/:agent_id", () => new HttpResponse(null, { status: 204 })),
  http.get("/v1/executions", ({ request }) => {
    const url = new URL(request.url);
    const agent = url.searchParams.get("agent_id");
    const status = url.searchParams.get("status");
    const items = [runFixture].filter(
      (r) =>
        (agent === null || r.agent_id === agent) &&
        (status === null || r.status === status),
    );
    return HttpResponse.json({ items });
  }),
  http.get("/v1/executions/:run_id", ({ params }) => {
    const { run_id } = params as { run_id: string };
    if (run_id === runFixture.run_id) {
      return HttpResponse.json(executionDetailFixture);
    }
    return HttpResponse.json(
      { error: { kind: "not_found", message: `execution ${run_id} not found` } },
      { status: 404 },
    );
  }),
  http.get("/v1/executions/:run_id/events", ({ params }) => {
    const { run_id } = params as { run_id: string };
    if (run_id === runFixture.run_id) {
      return HttpResponse.json({ run_id, after: null, events: eventLogFixture });
    }
    return HttpResponse.json(
      { error: { kind: "not_found", message: `execution ${run_id} not found` } },
      { status: 404 },
    );
  }),
  http.get("/v1/conversations/:agent_id/:session_id/messages", ({ params }) => {
    const { agent_id, session_id } = params as { agent_id: string; session_id: string };
    if (agent_id === agentFixture.id && session_id === "session-7") {
      return HttpResponse.json({
        agent_id,
        session_id,
        messages: [
          { role: "user", content: "what is 2+2?" },
          { role: "assistant", content: "It is 4." },
        ],
      });
    }
    return HttpResponse.json(
      {
        error: {
          kind: "not_found",
          message: `no conversation for agent ${agent_id} / session ${session_id}`,
        },
      },
      { status: 404 },
    );
  }),
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