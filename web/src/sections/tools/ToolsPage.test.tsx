import { http, HttpResponse } from "msw";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { TEST_CAPABILITIES, renderWithProviders } from "@/test/render";
import type { Capabilities } from "@/capabilities/types";
import { mcpProbeFixture, whoamiFixture } from "@/test/handlers";
import { server } from "@/test/msw";

import { ToolsPage } from "./ToolsPage";

/** Payload-swap: different backend, different registry — the page must follow. */
function capabilitiesWithTools(builtins: unknown[], mcp: unknown): Capabilities {
  const sections = { ...TEST_CAPABILITIES.sections };
  const tools = sections.tools;
  return {
    sections: {
      ...sections,
      tools: {
        ...tools,
        enabled: true,
        detail: { builtins, mcp },
      },
    },
  } as Capabilities;
}

describe("<ToolsPage/>", () => {
  it("renders the builtin registry from the capabilities payload", async () => {
    renderWithProviders(<ToolsPage />, { initialEntries: ["/tools"] });
    expect(await screen.findByText("calculator")).toBeInTheDocument();
    expect(screen.getByText("current_time")).toBeInTheDocument();
  });

  it("follows a swapped payload — no hardcoded tool names", async () => {
    renderWithProviders(<ToolsPage />, {
      initialEntries: ["/tools"],
      capabilities: capabilitiesWithTools(
        [{ name: "web_search", description: "Search the web.", parameters: { type: "object" } }],
        { enabled: false, stage: "S4" },
      ),
    });
    expect(await screen.findByText("web_search")).toBeInTheDocument();
    expect(screen.queryByText("calculator")).not.toBeInTheDocument();
  });

  it("shows an honest empty state when the registry is empty", async () => {
    renderWithProviders(<ToolsPage />, {
      initialEntries: ["/tools"],
      capabilities: capabilitiesWithTools([], { enabled: false, stage: "S4" }),
    });
    expect(await screen.findByText("No tools registered.")).toBeInTheDocument();
  });

  it("shows the coming-soon copy while the backend's MCP gate is closed", async () => {
    renderWithProviders(<ToolsPage />, {
      initialEntries: ["/tools"],
      capabilities: capabilitiesWithTools(
        [{ name: "calculator", description: "", parameters: {} }],
        { enabled: false, stage: "S4" },
      ),
    });
    expect(await screen.findByText(/MCP tools are not enabled yet/)).toBeInTheDocument();
  });
});

describe("<ToolsPage/> MCP panel (S4)", () => {
  it("lists the tenant's servers live from the listing route", async () => {
    renderWithProviders(<ToolsPage />, { initialEntries: ["/tools"] });
    expect(await screen.findByRole("heading", { name: "MCP servers" })).toBeInTheDocument();
    expect(await screen.findByText("fixtures")).toBeInTheDocument();
    expect(screen.getByText("stdio")).toBeInTheDocument();
    expect(screen.getByText("enabled")).toBeInTheDocument();
  });

  it("probes a server and renders its discovered tools like the builtins", async () => {
    const user = userEvent.setup();
    renderWithProviders(<ToolsPage />, { initialEntries: ["/tools"] });

    await user.click(await screen.findByRole("button", { name: "View tools" }));
    expect(await screen.findByText(mcpProbeFixture.tools[0]!.name)).toBeInTheDocument();
    expect(screen.getByText(mcpProbeFixture.tools[1]!.name)).toBeInTheDocument();
    // both probed tools render descriptor cards (the builtin calculator's
    // card makes the third "Parameter schema" on the page)
    expect(screen.getAllByText("Parameter schema").length).toBe(3);
  });

  it("surfaces a failed probe (502 envelope) as an honest error", async () => {
    server.use(
      http.post("/v1/mcp/servers/:server_id/probe", () =>
        HttpResponse.json(
          {
            error: {
              kind: "mcp_unreachable",
              message: "fixtures: connection failed",
            },
          },
          { status: 502 },
        ),
      ),
    );
    const user = userEvent.setup();
    renderWithProviders(<ToolsPage />, { initialEntries: ["/tools"] });

    await user.click(await screen.findByRole("button", { name: "View tools" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("connection failed");
  });

  it("adds a server through the dialog", async () => {
    const user = userEvent.setup();
    renderWithProviders(<ToolsPage />, { initialEntries: ["/tools"] });

    await user.click(await screen.findByRole("button", { name: "Add server" }));
    await user.type(screen.getByLabelText("Name (slug)"), "weather");
    await user.type(screen.getByLabelText("Command"), "uvx");
    await user.type(screen.getByLabelText("Arguments (comma-separated)"), "mcp-weather");
    await user.click(screen.getByRole("button", { name: "Create server" }));

    // the create round-trips and the panel confirms it
    expect(await screen.findByText("Added MCP server weather")).toBeInTheDocument();
  });

  it("keeps the mutating controls in anonymous mode — whoami is an object with role null, not null", async () => {
    // The backend's anonymous whoami answers 200 {mode:"anonymous", role:null}.
    // Treating that as "not a manager" hid the panel's buttons from the
    // single-user local mode that has full API access (found live).
    server.use(
      http.get("/v1/auth/whoami", () =>
        HttpResponse.json({
          tenant_id: "default",
          mode: "anonymous",
          user_id: null,
          email: null,
          display_name: "",
          role: null,
        }),
      ),
    );
    renderWithProviders(<ToolsPage />, { initialEntries: ["/tools"] });

    expect(await screen.findByText("fixtures")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add server" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Remove" })).toBeInTheDocument();
  });

  it("hides the mutating controls from a member — the API stays the enforcer", async () => {
    server.use(
      http.get("/v1/auth/whoami", () =>
        HttpResponse.json({ ...whoamiFixture, role: "member" }),
      ),
    );
    renderWithProviders(<ToolsPage />, { initialEntries: ["/tools"] });

    expect(await screen.findByText("fixtures")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Add server" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Remove" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Disable" })).not.toBeInTheDocument();
    // View tools stays — listing and probing are any-member
    expect(screen.getByRole("button", { name: "View tools" })).toBeInTheDocument();
  });

  it("confirms before deleting and says what a delete means for bound agents", async () => {
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
    const user = userEvent.setup();
    renderWithProviders(<ToolsPage />, { initialEntries: ["/tools"] });

    await user.click(await screen.findByRole("button", { name: "Remove" }));
    expect(confirmSpy).toHaveBeenCalledWith(
      expect.stringContaining("fail tool resolution"),
    );
    confirmSpy.mockRestore();
  });
});