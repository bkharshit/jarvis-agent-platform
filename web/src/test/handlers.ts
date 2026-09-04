import { http, HttpResponse } from "msw";

// Default handlers for the suite — happy-path stand-ins keyed to the real
// API surface. Per-test cases use server.use(...) on top of these.

export const handlers = [
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