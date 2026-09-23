import { http, HttpResponse } from "msw";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { agentFixture, credentialFixture, envelope } from "@/test/handlers";
import { server } from "@/test/msw";
import { renderWithProviders, TEST_CAPABILITIES } from "@/test/render";

import { AgentEditor } from "./AgentEditor";

type CapturedBody = Record<string, unknown>;

function capturePost() {
  const state: { body: CapturedBody | null } = { body: null };
  const capture = () => {
    server.use(
      http.post("/v1/agents", async ({ request }) => {
        state.body = (await request.json()) as CapturedBody;
        return HttpResponse.json({ definition: agentFixture, versions: [] }, { status: 201 });
      }),
    );
  };
  return { state, capture };
}

describe("<AgentEditor/> creating", () => {
  it("prefills Model from the capabilities environment defaults (ADR 0007)", async () => {
    renderWithProviders(<AgentEditor />, { initialEntries: ["/agents/new"] });
    const modelInput = await screen.findByLabelText("Model");
    await waitFor(() => expect(modelInput).toHaveValue("mock-agent"));
    expect(screen.getByLabelText("Provider")).toHaveValue("mock");
  });

  it("suggests the live model catalog in the Model datalist", async () => {
    renderWithProviders(<AgentEditor />, { initialEntries: ["/agents/new"] });
    await screen.findByLabelText("Model");
    const datalist = await screen.findByRole("listbox", { hidden: true }, { timeout: 2000 });
    const options = within(datalist).getAllByRole("option", { hidden: true });
    expect(options.map((o) => o.getAttribute("value"))).toEqual([
      "mock-small",
      "mock-large",
    ]);
  });

  it("degrades to free text when the model listing fails", async () => {
    server.use(
      http.get("/v1/models", () =>
        HttpResponse.json(
          envelope("model_unreachable", "connection refused"),
          { status: 502 },
        ),
      ),
    );
    const user = userEvent.setup();
    renderWithProviders(<AgentEditor />, { initialEntries: ["/agents/new"] });
    const modelInput = await screen.findByLabelText("Model");
    expect(await screen.findByText(/Model listing unavailable \(connection refused\)/))
      .toBeInTheDocument();
    await user.clear(modelInput); // the env-default prefill ran; typing replaces it
    await user.type(modelInput, "hand-typed-model");
    expect(modelInput).toHaveValue("hand-typed-model");
  });

  it("POSTs a body that omits empty optional fields (null-injection guard)", async () => {
    const user = userEvent.setup();
    const posted = capturePost();
    posted.capture();

    renderWithProviders(<AgentEditor />, { initialEntries: ["/agents/new"] });
    await user.type(await screen.findByLabelText("Name"), "My new agent");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(posted.state.body).not.toBeNull());
    const raw = JSON.parse(JSON.stringify(posted.state.body));
    expect(raw.name).toBe("My new agent");
    expect("user_prompt_template" in raw).toBe(false);
    expect("output_schema" in raw).toBe(false);
    expect("base_url" in raw.model).toBe(false);
  });

  it("surfaces 422 field errors inline", async () => {
    server.use(
      http.post("/v1/agents", () =>
        HttpResponse.json(
          envelope("validation", "request validation failed", {
            errors: [{ loc: ["body", "max_iterations"], msg: "less than or equal to 32" }],
          }),
          { status: 422 },
        ),
      ),
    );
    const user = userEvent.setup();
    renderWithProviders(<AgentEditor />, { initialEntries: ["/agents/new"] });
    await user.type(await screen.findByLabelText("Name"), "Over the limit");
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText("less than or equal to 32")).toBeInTheDocument();
  });

  it("toasts 409 conflicts with the server message verbatim", async () => {
    server.use(
      http.post("/v1/agents", () =>
        HttpResponse.json(envelope("conflict", "agent name already exists"), { status: 409 }),
      ),
    );
    const user = userEvent.setup();
    renderWithProviders(<AgentEditor />, { initialEntries: ["/agents/new"] });
    await user.type(await screen.findByLabelText("Name"), "Duplicate");
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText("agent name already exists")).toBeInTheDocument();
  });
});

describe("<AgentEditor/> credential binding (S2)", () => {
  it("picks a stored credential by name and saves its id as the credential_ref", async () => {
    const user = userEvent.setup();
    const posted = capturePost();
    posted.capture();

    renderWithProviders(<AgentEditor />, { initialEntries: ["/agents/new"] });
    await user.type(await screen.findByLabelText("Name"), "BYOK bound");
    await user.selectOptions(screen.getByLabelText("Credential (optional)"), "stored");
    // the dropdown lists the tenant's credentials — name + provider, value = id
    await user.selectOptions(
      screen.getByLabelText("Stored credential"),
      credentialFixture.id,
    );

    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(posted.state.body).not.toBeNull());
    const raw = JSON.parse(JSON.stringify(posted.state.body));
    expect(raw.model.credential_ref).toEqual({
      type: "stored",
      credential_id: credentialFixture.id,
    });
  });

  it("points at Settings when the tenant has no stored credentials yet", async () => {
    server.use(http.get("/v1/credentials", () => HttpResponse.json({ items: [] })));
    const user = userEvent.setup();
    renderWithProviders(<AgentEditor />, { initialEntries: ["/agents/new"] });
    await screen.findByLabelText("Name");
    await user.selectOptions(screen.getByLabelText("Credential (optional)"), "stored");
    expect(
      await screen.findByText(/No stored credentials yet — create one in Settings/),
    ).toBeInTheDocument();
  });

  it("falls back to a raw credential-id input when the listing fails", async () => {
    server.use(
      http.get("/v1/credentials", () =>
        HttpResponse.json(envelope("internal", "internal server error"), { status: 500 }),
      ),
    );
    const user = userEvent.setup();
    renderWithProviders(<AgentEditor />, { initialEntries: ["/agents/new"] });
    await screen.findByLabelText("Name");
    await user.selectOptions(screen.getByLabelText("Credential (optional)"), "stored");
    const fallback = screen.getByLabelText("Stored credential id");
    await user.type(fallback, "a01fa02c-53de-4b4e-a27d-46fbf210ea41");
    expect(fallback).toHaveValue("a01fa02c-53de-4b4e-a27d-46fbf210ea41");
  });
});

describe("<AgentEditor/> editing", () => {
  it("loads the definition and PATCHes the saved body", async () => {
    const bodies: CapturedBody[] = [];
    server.use(
      http.patch("/v1/agents/:agent_id", async ({ request }) => {
        bodies.push((await request.json()) as CapturedBody);
        return HttpResponse.json({ definition: agentFixture, versions: [] });
      }),
    );
    const user = userEvent.setup();
    renderWithProviders(<AgentEditor />, {
      initialEntries: ["/agents/agent-1/edit"],
      path: "/agents/:agentId/edit",
    });
    await user.type(await screen.findByLabelText("Name"), " renamed");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(bodies).toHaveLength(1));
    expect(bodies[0].name).toBe("Research agent renamed");
  });

  it("shows an honest not-found state for a missing agent", async () => {
    renderWithProviders(<AgentEditor />, {
      initialEntries: ["/agents/nope/edit"],
      path: "/agents/:agentId/edit",
    });
    expect(await screen.findByText("agent nope not found")).toBeInTheDocument();
  });
});
describe("<AgentEditor/> MCP tool picker (S4)", () => {
  it("probes the picked server and binds a checked tool as an approval-gated row", async () => {
    const user = userEvent.setup();
    renderWithProviders(<AgentEditor />, { initialEntries: ["/agents/new"] });
    await screen.findByLabelText("Model");

    await user.selectOptions(await screen.findByLabelText("Add MCP tool"), "mcp-1");
    const echo = await screen.findByRole("checkbox", { name: "Bind mcp__fixtures__echo" });
    await user.click(echo);

    // the binding row is an ordinary ToolBinding — approval default visible-on
    expect(
      screen.getByLabelText("mcp__fixtures__echo config (JSON)"),
    ).toBeInTheDocument();
    const approval = screen.getByRole("checkbox", { name: "mcp__fixtures__echo requires approval" });
    expect(approval).toBeChecked();

    // unchecking removes the binding again
    await user.click(screen.getByRole("checkbox", { name: "Bind mcp__fixtures__echo" }));
    expect(
      screen.queryByLabelText("mcp__fixtures__echo config (JSON)"),
    ).not.toBeInTheDocument();
  });

  it("toggling approval rewrites the binding config JSON", async () => {
    const user = userEvent.setup();
    renderWithProviders(<AgentEditor />, { initialEntries: ["/agents/new"] });
    await screen.findByLabelText("Model");

    await user.selectOptions(await screen.findByLabelText("Add MCP tool"), "mcp-1");
    await user.click(await screen.findByRole("checkbox", { name: "Bind mcp__fixtures__echo" }));

    const approval = screen.getByRole("checkbox", { name: "mcp__fixtures__echo requires approval" });
    await user.click(approval); // un-gate
    expect(approval).not.toBeChecked();
    expect(
      screen.getByLabelText("mcp__fixtures__echo config (JSON)"),
    ).toHaveValue('{"requires_approval":false}');
  });

  it("surfaces a failed probe without blocking the editor", async () => {
    server.use(
      http.post("/v1/mcp/servers/:server_id/probe", () =>
        HttpResponse.json(envelope("mcp_unreachable", "fixtures: connection refused"), {
          status: 502,
        }),
      ),
    );
    const user = userEvent.setup();
    renderWithProviders(<AgentEditor />, { initialEntries: ["/agents/new"] });
    await screen.findByLabelText("Model");

    await user.selectOptions(await screen.findByLabelText("Add MCP tool"), "mcp-1");
    expect(await screen.findByRole("alert")).toHaveTextContent("connection refused");
  });
});

describe("<AgentEditor/> sub-agent picker (S14, ADR 0018)", () => {
  it("binds a picked agent as a sub__ row carrying only the agent_id", async () => {
    const user = userEvent.setup();
    const posted = capturePost();
    posted.capture();

    renderWithProviders(<AgentEditor />, { initialEntries: ["/agents/new"] });
    await user.type(await screen.findByLabelText("Name"), "Supervisor");
    await screen.findByLabelText("Model");

    await user.selectOptions(await screen.findByLabelText("Delegate to sub-agent"), "agent-1");
    // the binding row is an ordinary ToolBinding; NO pin — the server stamps
    // agent_version_id at save (never trusted from the client)
    expect(
      screen.getByLabelText("sub__Research agent config (JSON)"),
    ).toHaveValue('{"agent_id":"agent-1"}');

    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(posted.state.body).not.toBeNull());
    const raw = JSON.parse(JSON.stringify(posted.state.body));
    expect(raw.tools).toEqual([
      { name: "sub__Research agent", enabled: true, config: { agent_id: "agent-1" } },
    ]);
  });

  it("binds a handoff target as a handoff__ row", async () => {
    const user = userEvent.setup();
    const posted = capturePost();
    posted.capture();

    renderWithProviders(<AgentEditor />, { initialEntries: ["/agents/new"] });
    await user.type(await screen.findByLabelText("Name"), "Relay");
    await screen.findByLabelText("Model");

    await user.selectOptions(await screen.findByLabelText("Hand off to agent"), "agent-1");
    expect(
      screen.getByLabelText("handoff__Research agent config (JSON)"),
    ).toHaveValue('{"agent_id":"agent-1"}');

    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(posted.state.body).not.toBeNull());
    const raw = JSON.parse(JSON.stringify(posted.state.body));
    expect(raw.tools).toEqual([
      { name: "handoff__Research agent", enabled: true, config: { agent_id: "agent-1" } },
    ]);
  });

  it("excludes the agent being edited (self-binding is a 422)", async () => {
    renderWithProviders(<AgentEditor />, {
      initialEntries: ["/agents/agent-1/edit"],
      path: "/agents/:agentId/edit",
    });
    const picker = await screen.findByLabelText("Delegate to sub-agent");
    // the only tenant agent IS the edited one — placeholder option only
    expect(within(picker).getAllByRole("option")).toHaveLength(1);
    expect(screen.queryByText("Research agent")).not.toBeInTheDocument();
  });

  it("is hidden when the sub_agents capability flag is off", async () => {
    renderWithProviders(<AgentEditor />, {
      initialEntries: ["/agents/new"],
      capabilities: {
        ...TEST_CAPABILITIES,
        sections: {
          ...TEST_CAPABILITIES.sections,
          agents: { enabled: true, summary: "Create, version, and run agents" },
        },
      },
    });
    await screen.findByLabelText("Model");
    expect(screen.queryByLabelText("Delegate to sub-agent")).not.toBeInTheDocument();
  });
});

describe("<AgentEditor/> knowledge picker (S8, ADR 0020)", () => {
  it("binds selected datasets by id and sends the binding on save", async () => {
    const user = userEvent.setup();
    const posted = capturePost();
    posted.capture();

    renderWithProviders(<AgentEditor />, { initialEntries: ["/agents/new"] });
    await user.type(await screen.findByLabelText("Name"), "Knowledgeable");
    await screen.findByLabelText("Model");

    const picker = await screen.findByTestId("knowledge-picker");
    const checkbox = within(picker).getByRole("checkbox");
    await user.click(checkbox);

    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(posted.state.body).not.toBeNull());
    const raw = JSON.parse(JSON.stringify(posted.state.body));
    // the binding carries the DATASET ID (never the name — the S14 rule)
    expect(raw.knowledge).toEqual({ datasets: ["kb-ds-1"], top_k: 4 });
  });

  it("omits the knowledge key entirely when nothing is selected", async () => {
    const user = userEvent.setup();
    const posted = capturePost();
    posted.capture();

    renderWithProviders(<AgentEditor />, { initialEntries: ["/agents/new"] });
    await user.type(await screen.findByLabelText("Name"), "Plain agent");
    await screen.findByLabelText("Model");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(posted.state.body).not.toBeNull());
    expect(posted.state.body).not.toHaveProperty("knowledge");
  });

  it("collects a custom top_k alongside the binding", async () => {
    const user = userEvent.setup();
    const posted = capturePost();
    posted.capture();

    renderWithProviders(<AgentEditor />, { initialEntries: ["/agents/new"] });
    await user.type(await screen.findByLabelText("Name"), "Narrow");
    await screen.findByLabelText("Model");
    const picker = await screen.findByTestId("knowledge-picker");
    await user.click(within(picker).getByRole("checkbox"));
    const topK = screen.getByLabelText("Top K (1–20)");
    await user.clear(topK);
    await user.type(topK, "2");

    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(posted.state.body).not.toBeNull());
    const raw = JSON.parse(JSON.stringify(posted.state.body));
    expect(raw.knowledge).toEqual({ datasets: ["kb-ds-1"], top_k: 2 });
  });

  it("is hidden while the tenant has no datasets", async () => {
    server.use(
      http.get("/v1/knowledge/datasets", () => HttpResponse.json({ items: [] })),
    );
    renderWithProviders(<AgentEditor />, { initialEntries: ["/agents/new"] });
    expect(await screen.findByLabelText("Name")).toBeInTheDocument();
    expect(screen.queryByTestId("knowledge-picker")).not.toBeInTheDocument();
  });
});
