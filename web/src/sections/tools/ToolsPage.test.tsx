import { http, HttpResponse } from "msw";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { TEST_CAPABILITIES, renderWithProviders } from "@/test/render";
import type { Capabilities } from "@/capabilities/types";
import {
  credentialFixture,
  mcpHttpServerFixture,
  mcpProbeFixture,
  mcpServerFixture,
  storedCredentialFixture,
  whoamiFixture,
} from "@/test/handlers";
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
describe("<ToolsPage/> header refs (ADR 0013)", () => {
  async function openHttpForm(user: ReturnType<typeof userEvent.setup>) {
    await user.click(await screen.findByRole("button", { name: "Add server" }));
    await user.type(await screen.findByLabelText("Name (slug)"), "webz-news");
    await user.selectOptions(screen.getByLabelText("Transport"), "http");
    await user.type(screen.getByLabelText("URL"), "https://news-search-mcp.webz.io/mcp");
    await user.click(screen.getByRole("button", { name: "＋ Add header" }));
  }

  it("binds a header to a stored credential picked from the tenant's credentials", async () => {
    const captured: { body: Record<string, unknown> | null } = { body: null };
    server.use(
      http.get("/v1/credentials", () =>
        HttpResponse.json({ items: [credentialFixture, storedCredentialFixture] }),
      ),
      http.post("/v1/mcp/servers", async ({ request }) => {
        captured.body = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(mcpHttpServerFixture, { status: 201 });
      }),
    );
    const user = userEvent.setup();
    renderWithProviders(<ToolsPage />, { initialEntries: ["/tools"] });
    await openHttpForm(user);

    // the stored option exists signed-in; the picker lists credentials by name
    await user.selectOptions(
      await screen.findByLabelText("Value source for header 1"),
      "stored",
    );
    await user.selectOptions(screen.getByLabelText("Credential for header 1"), "cred-2");
    await user.type(screen.getByLabelText("Header name 1"), "Authorization");
    await user.click(screen.getByRole("button", { name: "Create server" }));

    expect(await screen.findByText("Added MCP server webz-news")).toBeInTheDocument();
    const config = (captured.body?.config ?? {}) as Record<string, unknown>;
    expect(config.headers).toEqual({
      Authorization: { type: "stored", credential_id: "cred-2" },
    });
  });

  it("creates a credential inline (sent once, provider mcp_header) and selects its id", async () => {
    const credentialBodies: Record<string, unknown>[] = [];
    let credentials: object[] = []; // the tenant starts with no credentials
    server.use(
      http.post("/v1/credentials", async ({ request }) => {
        credentialBodies.push((await request.json()) as Record<string, unknown>);
        credentials = [credentialFixture, storedCredentialFixture]; // the refetch sees it
        return HttpResponse.json(storedCredentialFixture, { status: 201 });
      }),
      http.get("/v1/credentials", () => HttpResponse.json({ items: credentials })),
    );
    const user = userEvent.setup();
    renderWithProviders(<ToolsPage />, { initialEntries: ["/tools"] });
    await openHttpForm(user);

    await user.selectOptions(await screen.findByLabelText("Value source for header 1"), "stored");
    await user.click(screen.getByRole("button", { name: "＋ New credential" }));
    await user.type(screen.getByLabelText("New credential name"), "webz-key");
    await user.type(screen.getByLabelText("New credential secret"), "Bearer sk_webz_999");
    await user.click(screen.getByRole("button", { name: "Save credential" }));

    // the secret goes to the credentials route exactly once, never to the MCP create
    expect(credentialBodies).toEqual([
      { name: "webz-key", provider: "mcp_header", secret: "Bearer sk_webz_999" },
    ]);
    // the created id is auto-selected into the ref (it was blank — no rows yet)
    await waitFor(() =>
      expect(screen.getByLabelText("Credential for header 1")).toHaveValue("cred-2"),
    );
  });

  it("offers env-var refs only in anonymous mode — the stored option is absent, not disabled", async () => {
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
    const user = userEvent.setup();
    renderWithProviders(<ToolsPage />, { initialEntries: ["/tools"] });
    await openHttpForm(user);

    const source = await screen.findByLabelText("Value source for header 1");
    const options = Array.from(source.querySelectorAll("option")).map((o) => o.value);
    expect(options).toEqual(["env"]);
    expect(
      screen.getByText(/Storing API keys as encrypted credentials requires signing in/),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "＋ New credential" })).not.toBeInTheDocument();
  });
});

describe("<ToolsPage/> edit refs + rotate (ADR 0013)", () => {
  /** The listing carries the http row (the default handler lists stdio only). */
  function listHttpServer() {
    server.use(
      http.get("/v1/mcp/servers", () =>
        HttpResponse.json({ items: [mcpHttpServerFixture] }),
      ),
      http.get("/v1/credentials", () =>
        HttpResponse.json({ items: [credentialFixture, storedCredentialFixture] }),
      ),
    );
  }

  it("edits an http server's headers and PATCHes the FULL config — url survives", async () => {
    listHttpServer();
    const patches: Record<string, unknown>[] = [];
    server.use(
      http.patch("/v1/mcp/servers/:server_id", async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>;
        patches.push(body);
        return HttpResponse.json({
          ...mcpHttpServerFixture,
          config: body.config as typeof mcpHttpServerFixture.config,
        });
      }),
    );
    const user = userEvent.setup();
    renderWithProviders(<ToolsPage />, { initialEntries: ["/tools"] });

    await user.click(await screen.findByRole("button", { name: "Edit headers" }));
    // Untouched editor never PATCHes — the submit stays disabled.
    expect(screen.getByRole("button", { name: "Save refs" })).toBeDisabled();

    // The X-Api-Key env ref is the second entry (Authorization is first).
    const envVar = screen.getByLabelText("Env variable for header 2");
    await user.clear(envVar);
    await user.type(envVar, "WEBZ_MCP_TOKEN_V2");
    await user.click(screen.getByRole("button", { name: "Save refs" }));

    expect(await screen.findByText("Updated refs for webz-news")).toBeInTheDocument();
    expect(patches).toHaveLength(1);
    const config = patches[0]!.config as Record<string, unknown>;
    // wholesale replace (routes/mcp.py) — the full config must ride along
    expect(config.url).toBe(mcpHttpServerFixture.config.url);
    expect(config.headers).toEqual({
      Authorization: { type: "stored", credential_id: "cred-2" },
      "X-Api-Key": { type: "env", env_var: "WEBZ_MCP_TOKEN_V2" },
    });
  });

  it("edits a stdio server's env refs and PATCHes the FULL config — command survives", async () => {
    const patches: Record<string, unknown>[] = [];
    server.use(
      http.patch("/v1/mcp/servers/:server_id", async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>;
        patches.push(body);
        return HttpResponse.json({
          ...mcpServerFixture,
          config: body.config as typeof mcpServerFixture.config,
        });
      }),
    );
    const user = userEvent.setup();
    renderWithProviders(<ToolsPage />, { initialEntries: ["/tools"] });

    await user.click(await screen.findByRole("button", { name: "Edit env" }));
    await user.click(screen.getByRole("button", { name: "＋ Add header" }));
    await user.type(screen.getByLabelText("Header name 1"), "WEATHER_API_KEY");
    await user.type(screen.getByLabelText("Env variable for header 1"), "WEATHER_API_KEY");
    await user.click(screen.getByRole("button", { name: "Save refs" }));

    expect(await screen.findByText("Updated refs for fixtures")).toBeInTheDocument();
    const config = patches[0]!.config as Record<string, unknown>;
    expect(config.command).toBe("uvx"); // the wholesale-replace gotcha, stdio side
    expect(config.args).toEqual(["mcp-server-time"]);
    expect(config.env).toEqual({
      WEATHER_API_KEY: { type: "env", env_var: "WEATHER_API_KEY" },
    });
  });

  it("rotates a stored credential's secret from the row — sent once, never echoed", async () => {
    listHttpServer();
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
    const patches: Record<string, unknown>[] = [];
    server.use(
      http.patch("/v1/credentials/:credential_id", async ({ request }) => {
        patches.push((await request.json()) as Record<string, unknown>);
        return HttpResponse.json(credentialFixture);
      }),
    );
    const user = userEvent.setup();
    renderWithProviders(<ToolsPage />, { initialEntries: ["/tools"] });

    // The stored ref renders with the credential's NAME (from the list, not the id)
    expect(await screen.findByText(/→ webz-key/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Rotate secret" }));
    await user.type(
      screen.getByLabelText("New secret for webz-key"),
      "Bearer sk_rotated_42",
    );
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByText("Rotated the secret for webz-key")).toBeInTheDocument();
    expect(confirmSpy).toHaveBeenCalledWith(expect.stringContaining("webz-key"));
    expect(patches).toEqual([{ secret: "Bearer sk_rotated_42" }]);
    confirmSpy.mockRestore();
  });
});
