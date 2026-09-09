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

/** The signed-in principal matching the settings fixtures below. */
export const whoamiFixture = {
  tenant_id: "default",
  mode: "session",
  user_id: "user-1",
  email: "owner@acme.test",
  display_name: "Owner",
  role: "owner",
};

export const memberFixture = {
  id: "user-2",
  tenant_id: "default",
  email: "member@acme.test",
  display_name: "Member Two",
  role: "member",
  created_at: "2026-09-05T09:00:00Z",
};

export const apiKeyFixture = {
  id: "key-1",
  tenant_id: "default",
  user_id: "user-1",
  name: "ci",
  key_prefix: "jarvis_sk_1a2b3c4d5e6f",
  created_at: "2026-09-05T09:00:00Z",
  last_used_at: null,
  revoked_at: null,
};

/** Create-time response: the ONLY payload that ever carries the plaintext. */
export const apiKeyCreatedFixture = {
  id: "key-2",
  name: "cli key",
  key_prefix: "jarvis_sk_9f8e7d6c5b4a",
  plaintext: "jarvis_sk_9f8e7d6c5b4a3210fedcba9876543210fedcba9876543210abcd",
  created_at: "2026-09-05T10:00:00Z",
};

export const credentialFixture = {
  id: "cred-1",
  tenant_id: "default",
  name: "prod key",
  provider: "openai_compatible",
  created_by: "user-1",
  created_at: "2026-09-05T09:00:00Z",
  updated_at: null,
  revoked_at: null,
};

/** An MCP server row (S4, ADR 0012) — env refs carry NAMES only. */
export const mcpServerFixture = {
  id: "mcp-1",
  name: "fixtures",
  config: { type: "stdio" as const, command: "uvx", args: ["mcp-server-time"] },
  enabled: true,
  tenant_id: "default",
  created_at: "2026-09-08T09:00:00Z",
  updated_at: "2026-09-08T09:00:00Z",
};

/** A header-auth http MCP server (ADR 0013) — refs only, never a secret. */
export const mcpHttpServerFixture = {
  id: "mcp-http-1",
  name: "webz-news",
  config: {
    type: "http" as const,
    url: "https://news-search-mcp.webz.io/mcp",
    headers: {
      Authorization: { type: "stored" as const, credential_id: "cred-2" },
      "X-Api-Key": { type: "env" as const, env_var: "WEBZ_MCP_TOKEN" },
    },
  },
  enabled: true,
  tenant_id: "default",
  created_at: "2026-09-09T09:00:00Z",
  updated_at: "2026-09-09T09:00:00Z",
};

/** A BYOK credential holding an MCP header secret (provider metadata only). */
export const storedCredentialFixture = {
  ...credentialFixture,
  id: "cred-2",
  name: "webz-key",
  provider: "mcp_header",
};

/** The probe's descriptor shape — full JARVIS names, approval default on. */
export const mcpProbeFixture = {
  server: mcpServerFixture,
  tools: [
    {
      name: "mcp__fixtures__echo",
      description: "Echo the given text back.",
      parameters: {
        type: "object",
        properties: { text: { type: "string" } },
        required: ["text"],
      },
      annotations: { requires_approval: true, timeout: null },
    },
    {
      name: "mcp__fixtures__add_numbers",
      description: "Add two numbers.",
      parameters: {
        type: "object",
        properties: { a: { type: "number" }, b: { type: "number" } },
        required: ["a", "b"],
      },
      annotations: { requires_approval: true, timeout: null },
    },
  ],
};

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
  http.get("/v1/models", ({ request }) => {
    const url = new URL(request.url);
    const provider = url.searchParams.get("provider") ?? "openai_compatible";
    const models = provider === "mock"
      ? ["mock-small", "mock-large"]
      : ["endpoint-model-a", "endpoint-model-b"];
    return HttpResponse.json({
      provider,
      base_url: url.searchParams.get("base_url"),
      models,
    });
  }),
  http.get("/v1/capabilities", () => {
    return HttpResponse.json({
      sections: {
        agents: { enabled: true, summary: "Create, version, and run agents", detail: { strategies: ["react", "function_calling"] } },
        executions: { enabled: true, summary: "Browse runs", detail: { human_in_the_loop: true } },
        conversations: { enabled: true, summary: "Per-session history" },
        tools: {
          enabled: true,
          summary: "Builtin registry",
          detail: {
            builtins: [
              {
                name: "calculator",
                description: "Evaluate an arithmetic expression.",
                parameters: {
                  type: "object",
                  properties: { expression: { type: "string" } },
                  required: ["expression"],
                },
              },
              { name: "current_time", description: "Current UTC time.", parameters: {} },
            ],
            mcp: {
              enabled: true,
              servers: [
                {
                  id: mcpServerFixture.id,
                  name: mcpServerFixture.name,
                  transport: mcpServerFixture.config.type,
                  enabled: mcpServerFixture.enabled,
                },
              ],
            },
          },
        },
        models: {
          enabled: true,
          mode: "read-only",
          summary: "Provider info",
          detail: {
            providers: [
              {
                name: "mock",
                description: "Scripted provider for tests and demos (no network)",
                capabilities: { streaming: true, function_calling: true, structured_output: "json_schema", parallel_tool_calls: false },
              },
              {
                name: "openai_compatible",
                description: "Any OpenAI-compatible endpoint via base_url",
                capabilities: { streaming: true, function_calling: true, structured_output: "json_schema", parallel_tool_calls: true },
              },
            ],
            defaults: { provider: "mock", model: "mock-agent", base_url: null },
          },
        },
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
        settings: {
          enabled: true,
          summary: "Auth, tenants, API keys, BYOK credentials",
          detail: { auth_mode: "required", credentials: { available: true } },
        },
      },
    });
  }),
  // --- auth (S2) ----------------------------------------------------------
  http.get("/v1/auth/whoami", () => HttpResponse.json(whoamiFixture)),
  http.post("/v1/auth/login", () => HttpResponse.json(whoamiFixture)),
  http.post("/v1/auth/logout", () => new HttpResponse(null, { status: 204 })),
  // --- members ------------------------------------------------------------
  http.get("/v1/members", () => HttpResponse.json({ items: [memberFixture] })),
  http.post("/v1/members", () => HttpResponse.json(memberFixture, { status: 201 })),
  http.patch("/v1/members/:user_id", () => HttpResponse.json(memberFixture)),
  http.delete("/v1/members/:user_id", () => new HttpResponse(null, { status: 204 })),
  // --- API keys -----------------------------------------------------------
  http.get("/v1/api-keys", () => HttpResponse.json({ items: [apiKeyFixture] })),
  http.post("/v1/api-keys", () => HttpResponse.json(apiKeyCreatedFixture, { status: 201 })),
  http.delete("/v1/api-keys/:key_id", () => new HttpResponse(null, { status: 204 })),
  // --- MCP servers (S4) -----------------------------------------------------
  http.get("/v1/mcp/servers", () => HttpResponse.json({ items: [mcpServerFixture] })),
  http.post("/v1/mcp/servers", () =>
    HttpResponse.json(mcpServerFixture, { status: 201 }),
  ),
  http.get("/v1/mcp/servers/:server_id", ({ params }) => {
    const { server_id } = params as { server_id: string };
    if (server_id === mcpServerFixture.id) {
      return HttpResponse.json(mcpServerFixture);
    }
    return HttpResponse.json(
      { error: { kind: "not_found", message: `MCP server ${server_id} not found` } },
      { status: 404 },
    );
  }),
  // PATCH replaces config wholesale on the backend — echo the body's config
  // so a test can catch the UI sending a partial config (which would wipe
  // url/command/args server-side).
  http.patch("/v1/mcp/servers/:server_id", async ({ request }) => {
    const body = (await request.json()) as { config?: Record<string, unknown> };
    if (body.config === undefined) return HttpResponse.json(mcpServerFixture);
    return HttpResponse.json({ ...mcpServerFixture, config: body.config });
  }),
  http.delete("/v1/mcp/servers/:server_id", () => new HttpResponse(null, { status: 204 })),
  http.post("/v1/mcp/servers/:server_id/probe", () =>
    HttpResponse.json(mcpProbeFixture),
  ),
  // --- BYOK credentials -----------------------------------------------------
  http.get("/v1/credentials", () => HttpResponse.json({ items: [credentialFixture] })),
  // The inline MCP credential create echoes the posted body (the created
  // id is what the header picker then selects).
  http.post("/v1/credentials", async ({ request }) => {
    const body = (await request.json()) as { name?: string };
    return HttpResponse.json(
      { ...storedCredentialFixture, name: body.name ?? storedCredentialFixture.name },
      { status: 201 },
    );
  }),
  http.patch("/v1/credentials/:credential_id", () => HttpResponse.json(credentialFixture)),
  http.delete("/v1/credentials/:credential_id", () => new HttpResponse(null, { status: 204 })),
];

/** Build a frozen-shape error envelope body (D13/D16). */
export function envelope(
  kind: string,
  message: string,
  details?: unknown,
): { error: { kind: string; message: string; details?: unknown } } {
  return { error: { kind, message, ...(details !== undefined ? { details } : {}) } };
}