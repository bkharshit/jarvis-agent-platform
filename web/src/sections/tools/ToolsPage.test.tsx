import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { TEST_CAPABILITIES, renderWithProviders } from "@/test/render";
import type { Capabilities } from "@/capabilities/types";

import { ToolsPage } from "./ToolsPage";

/** Payload-swap: different backend, different registry — the page must follow. */
function capabilitiesWithBuiltins(builtins: unknown[]): Capabilities {
  const sections = { ...TEST_CAPABILITIES.sections };
  const tools = sections.tools;
  return {
    sections: {
      ...sections,
      tools: {
        ...tools,
        enabled: true,
        detail: { builtins, mcp: { enabled: false, stage: "S4" } },
      },
    },
  } as Capabilities;
}

describe("<ToolsPage/>", () => {
  it("renders the builtin registry from the capabilities payload", async () => {
    renderWithProviders(<ToolsPage />, { initialEntries: ["/tools"] });
    expect(await screen.findByText("calculator")).toBeInTheDocument();
    expect(screen.getByText("current_time")).toBeInTheDocument();
    expect(screen.getByText(/MCP tools are not enabled yet/)).toBeInTheDocument();
  });

  it("follows a swapped payload — no hardcoded tool names", async () => {
    renderWithProviders(<ToolsPage />, {
      initialEntries: ["/tools"],
      capabilities: capabilitiesWithBuiltins([
        { name: "web_search", description: "Search the web.", parameters: { type: "object" } },
      ]),
    });
    expect(await screen.findByText("web_search")).toBeInTheDocument();
    expect(screen.queryByText("calculator")).not.toBeInTheDocument();
  });

  it("shows an honest empty state when the registry is empty", async () => {
    renderWithProviders(<ToolsPage />, {
      initialEntries: ["/tools"],
      capabilities: capabilitiesWithBuiltins([]),
    });
    expect(await screen.findByText("No tools registered.")).toBeInTheDocument();
  });
});