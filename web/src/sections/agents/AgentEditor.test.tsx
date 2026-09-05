import { http, HttpResponse } from "msw";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { agentFixture, envelope } from "@/test/handlers";
import { server } from "@/test/msw";
import { renderWithProviders } from "@/test/render";

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