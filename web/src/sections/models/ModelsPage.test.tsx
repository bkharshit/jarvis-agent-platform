import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { TEST_CAPABILITIES, renderWithProviders } from "@/test/render";
import type { Capabilities } from "@/capabilities/types";

import { ModelsPage } from "./ModelsPage";

function capabilitiesWithProviders(providers: unknown[], defaults?: unknown): Capabilities {
  const sections = { ...TEST_CAPABILITIES.sections };
  const models = sections.models;
  return {
    sections: {
      ...sections,
      models: {
        ...models,
        enabled: true,
        mode: "read-only",
        detail: {
          providers,
          defaults: defaults ?? { provider: "mock", model: "mock-agent", base_url: null },
        },
      },
    },
  } as Capabilities;
}

describe("<ModelsPage/>", () => {
  it("renders providers, capability badges, and defaults from the payload", async () => {
    renderWithProviders(<ModelsPage />, { initialEntries: ["/models"] });
    expect((await screen.findAllByText("mock")).length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("openai_compatible")).toBeInTheDocument();
    expect(screen.getByText("read-only")).toBeInTheDocument();
    expect(screen.getByText("parallel_tool_calls: yes")).toBeInTheDocument();
    expect(screen.getByText("mock-agent")).toBeInTheDocument();
  });

  it("follows a swapped payload — no hardcoded providers", async () => {
    renderWithProviders(<ModelsPage />, {
      initialEntries: ["/models"],
      capabilities: capabilitiesWithProviders([
        {
          name: "vllm",
          description: "Self-hosted vLLM endpoint",
          capabilities: { streaming: false, function_calling: true },
        },
      ]),
    });
    expect(await screen.findByText("vllm")).toBeInTheDocument();
    expect(screen.getByText("streaming: no")).toBeInTheDocument();
    expect(screen.queryByText("openai_compatible")).not.toBeInTheDocument();
    expect(screen.getByText("Environment defaults")).toBeInTheDocument();
  });
});