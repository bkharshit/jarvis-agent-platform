import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import type { ReactElement } from "react";
import { MemoryRouter, Route, Routes } from "react-router";

import { Toaster } from "@/components/Toaster";
import type { Capabilities } from "@/capabilities/types";
import { capabilitiesQueryKey } from "@/capabilities/useCapabilities";

export const TEST_CAPABILITIES: Capabilities = {
  sections: {
    agents: { enabled: true, summary: "Create, version, and run agents" },
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
        mcp: { enabled: false, stage: "S4" },
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