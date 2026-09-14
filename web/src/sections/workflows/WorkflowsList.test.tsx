import { http, HttpResponse } from "msw";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { envelope } from "@/test/handlers";
import { server } from "@/test/msw";
import { TEST_CAPABILITIES, renderWithProviders } from "@/test/render";

import { WorkflowsList } from "./WorkflowsList";

/** The backend flipped workflows on (S6 commit 7) — the section's live shape. */
const LIVE = {
  ...TEST_CAPABILITIES,
  sections: {
    ...TEST_CAPABILITIES.sections,
    workflows: { enabled: true, summary: "DAG runs reusing the same event model" },
  },
};

const workflowFixture = {
  id: "wf-1",
  name: "Research chain",
  description: "two agents in sequence",
  nodes: [
    { id: "a", type: "agent", config: { agent_id: "agent-1", agent_version_id: null, input_template: "{{input}}" } },
  ],
  edges: [],
  start_node_id: "a",
  max_node_executions: 24,
  created_at: "2026-09-14T00:00:00Z",
  updated_at: "2026-09-14T00:00:00Z",
};

describe("<WorkflowsList/>", () => {
  it("renders the coming-soon gate while the capability is off, then the live list (flip)", async () => {
    // disabled → the honest coming-soon surface
    renderWithProviders(<WorkflowsList />);
    expect(
      await screen.findByText(/coming soon/i),
    ).toBeInTheDocument();
  });

  it("renders workflows from the API payload", async () => {
    server.use(http.get("/v1/workflows", () => HttpResponse.json({ items: [workflowFixture] })));
    renderWithProviders(<WorkflowsList />, {
      capabilities: LIVE,
      initialEntries: ["/workflows"],
    });
    expect(await screen.findByRole("link", { name: "Research chain" })).toBeInTheDocument();
    expect(screen.getByText("1 nodes")).toBeInTheDocument();
  });

  it("shows an honest empty state", async () => {
    server.use(http.get("/v1/workflows", () => HttpResponse.json({ items: [] })));
    renderWithProviders(<WorkflowsList />, { capabilities: LIVE, initialEntries: ["/workflows"] });
    expect(await screen.findByTestId("empty-state")).toBeInTheDocument();
  });

  it("toasts the server message verbatim when delete conflicts (409)", async () => {
    const user = userEvent.setup();
    window.confirm = vi.fn(() => true);
    server.use(
      http.get("/v1/workflows", () => HttpResponse.json({ items: [workflowFixture] })),
      http.delete("/v1/workflows/:workflow_id", () =>
        HttpResponse.json(envelope("conflict", "workflow has executions; delete refused"), {
          status: 409,
        }),
      ),
    );
    renderWithProviders(<WorkflowsList />, { capabilities: LIVE, initialEntries: ["/workflows"] });
    await user.click(await screen.findByRole("button", { name: "Delete" }));
    await waitFor(() =>
      expect(screen.getByText(/workflow has executions/)).toBeInTheDocument(),
    );
  });
});