import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { TEST_CAPABILITIES, renderWithProviders } from "@/test/render";
import type { Capabilities } from "@/capabilities/types";

import { PluginsPage } from "./PluginsPage";

/** Payload-swap: different backend, different registry — the page must follow. */
function capabilitiesWithPlugins(plugins: unknown): Capabilities {
  const sections = { ...TEST_CAPABILITIES.sections };
  return {
    sections: { ...sections, plugins } as typeof sections,
  } as Capabilities;
}

describe("<PluginsPage/>", () => {
  it("renders the strategy listing with origin and distribution", async () => {
    renderWithProviders(<PluginsPage />, { initialEntries: ["/plugins"] });
    expect(await screen.findByText("function_calling")).toBeInTheDocument();
    // the plugin name appears in the listing AND in the allow-list panel
    expect(screen.getAllByText("plan_execute").length).toBe(2);
    expect(screen.getAllByText("builtin")).toHaveLength(2);
    expect(screen.getByText("jarvis-strategy-fixtures 0.1.0")).toBeInTheDocument();
  });

  it("follows a swapped payload — no hardcoded strategy names", async () => {
    renderWithProviders(<PluginsPage />, {
      initialEntries: ["/plugins"],
      capabilities: capabilitiesWithPlugins({
        enabled: true,
        summary: "Strategy plugins",
        detail: {
          strategies: [
            { name: "tree_of_thought", origin: "plugin", distribution: "tot-pkg", version: "2.0", error: null },
          ],
          failed: [],
          missing: [],
          allowlist: ["tree_of_thought"],
        },
      }),
    });
    expect(screen.getAllByText("tree_of_thought").length).toBe(2);
    expect(screen.queryByText("plan_execute")).not.toBeInTheDocument();
  });

  it("reports failed imports and missing allow-listed names", async () => {
    renderWithProviders(<PluginsPage />, {
      initialEntries: ["/plugins"],
      capabilities: capabilitiesWithPlugins({
        enabled: true,
        summary: "Strategy plugins",
        detail: {
          strategies: [],
          failed: [{ name: "broken", origin: "plugin", distribution: null, version: null, error: "ImportError: boom" }],
          missing: ["ghost"],
          allowlist: ["ghost", "broken"],
        },
      }),
    });
    expect(await screen.findByText("Failed imports")).toBeInTheDocument();
    expect(screen.getByText("ImportError: boom")).toBeInTheDocument();
    expect(screen.getByText("Allow-listed but not installed")).toBeInTheDocument();
    expect(screen.getByText("ghost")).toBeInTheDocument();
  });

  it("shows the honest empty-allow-list state with the env-var instructions", async () => {
    renderWithProviders(<PluginsPage />, {
      initialEntries: ["/plugins"],
      capabilities: capabilitiesWithPlugins({
        enabled: true,
        summary: "Strategy plugins",
        detail: { strategies: [], failed: [], missing: [], allowlist: [] },
      }),
    });
    expect(
      await screen.findByText("Empty — no plugins load. Nothing loads unless it is named here."),
    ).toBeInTheDocument();
    expect(screen.getByText(/JARVIS_STRATEGY_PLUGIN_ALLOWLIST/)).toBeInTheDocument();
  });
});