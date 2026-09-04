import { http, HttpResponse } from "msw";
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { server } from "@/test/msw";
import { renderWithProviders } from "@/test/render";

import { AgentDetailPage } from "./AgentDetailPage";

function renderDetail() {
  return renderWithProviders(<AgentDetailPage />, {
    initialEntries: ["/agents/agent-1"],
    path: "/agents/:agentId",
  });
}

describe("<AgentDetailPage/>", () => {
  it("shows the definition tab by default", async () => {
    renderDetail();
    expect(await screen.findByRole("heading", { name: "Research agent" })).toBeInTheDocument();
    expect(screen.getByText("mock/mock-agent", { exact: false })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Definition" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });

  it("lists versions from AgentDetail.versions and loads a snapshot on click", async () => {
    const user = userEvent.setup();
    renderDetail();

    await user.click(await screen.findByRole("tab", { name: "Versions" }));
    const table = screen.getByRole("table");
    expect(within(table).getByText("v1")).toBeInTheDocument();
    expect(within(table).getByText("initial")).toBeInTheDocument();

    // Snapshot loads into the side panel from the real endpoint.
    await user.click(within(table).getByRole("button", { name: "v1" }));
    expect(await screen.findByText(/Snapshot/)).toBeInTheDocument();
    expect(screen.getByText(/immutable/)).toBeInTheDocument();
    expect(screen.getByText(/"mock-agent"/)).toBeInTheDocument();
  });

  it("shows a 404 snapshot error honestly", async () => {
    server.use(
      http.get("/v1/agents/:agent_id/versions/:version", () =>
        HttpResponse.json(
          { error: { kind: "not_found", message: "version 1 not found" } },
          { status: 404 },
        ),
      ),
    );
    const user = userEvent.setup();
    renderDetail();

    await user.click(await screen.findByRole("tab", { name: "Versions" }));
    await user.click(screen.getByRole("button", { name: "v1" }));
    expect(await screen.findByText("version 1 not found")).toBeInTheDocument();
  });

  it("links runs to the executions section filtered by agent", async () => {
    const user = userEvent.setup();
    renderDetail();

    await user.click(await screen.findByRole("tab", { name: "Runs" }));
    expect(screen.getByRole("link", { name: "View runs →" })).toHaveAttribute(
      "href",
      "/executions?agent=agent-1",
    );
  });

  it("renders the API access curl snippet", async () => {
    const user = userEvent.setup();
    renderDetail();

    await user.click(await screen.findByRole("tab", { name: "API access" }));
    expect(screen.getByText(/\/v1\/agents\/agent-1\/run/)).toBeInTheDocument();
    expect(screen.getByText(/no keys in the payload/)).toBeInTheDocument();
  });
});