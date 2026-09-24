import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import type { ReactElement } from "react";
import { MemoryRouter, Route, Routes } from "react-router";

import { Toaster } from "@/components/Toaster";
import type { Capabilities } from "@/capabilities/types";
import { capabilitiesQueryKey } from "@/capabilities/useCapabilities";

export const TEST_CAPABILITIES: Capabilities = {
  sections: {
    agents: {
      enabled: true,
      summary: "Create, version, and run agents",
      detail: { strategies: ["react", "function_calling"], sub_agents: true },
    },
    executions: { enabled: true, summary: "Browse runs" },
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
            { id: "mcp-1", name: "fixtures", transport: "stdio", enabled: true },
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
    knowledge: {
      enabled: true,
      summary: "Datasets, documents, and retrieval",
      detail: {
        datasets: 1,
        documents: [{ id: "kb-ds-1", name: "Product notes", count: 2 }],
        agents_can_bind: true,
      },
    },
    evaluations: { enabled: true, summary: "Datasets, runs, and scores" },
    observability: {
      enabled: true,
      summary: "Traces and spans",
      detail: { spans: true, otel_export: false, service_name: "jarvis" },
    },
    plugins: {
      enabled: true,
      summary: "Strategy plugins and discovery",
      detail: {
        strategies: [
          { name: "function_calling", origin: "builtin", distribution: null, version: null, error: null },
          { name: "react", origin: "builtin", distribution: null, version: null, error: null },
          {
            name: "plan_execute",
            origin: "plugin",
            distribution: "jarvis-strategy-fixtures",
            version: "0.1.0",
            error: null,
          },
        ],
        failed: [],
        missing: [],
        allowlist: ["plan_execute"],
      },
    },
    triggers: {
      enabled: true,
      summary: "Cron, webhook, and event rules",
      detail: { total: 2, cron: 1, webhook: 1, event: 0 },
    },
    settings: {
      enabled: true,
      summary: "Auth, tenants, API keys, BYOK credentials",
      detail: { auth_mode: "required", credentials: { available: true } },
    },
  },
};

export function seedCapabilities(
  client: QueryClient,
  capabilities: Capabilities = TEST_CAPABILITIES,
): void {
  client.setQueryData([...capabilitiesQueryKey], capabilities);
}

export function renderWithProviders(
  ui: ReactElement,
  options?: {
    capabilities?: Capabilities | null;
    initialEntries?: string[];
    /** Route pattern to mount `ui` under, so useParams works (e.g. "/agents/:agentId/edit"). */
    path?: string;
  },
) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
  });
  if (options?.capabilities !== null) {
    seedCapabilities(client, options?.capabilities ?? TEST_CAPABILITIES);
  }
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={options?.initialEntries ?? ["/"]}>
        {options?.path ? (
          <Routes>
            <Route path={options.path} element={ui} />
          </Routes>
        ) : (
          ui
        )}
        <Toaster />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}