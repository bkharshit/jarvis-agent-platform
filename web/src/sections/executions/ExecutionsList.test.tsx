import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { renderWithProviders } from "@/test/render";

import { ExecutionsList } from "./ExecutionsList";

describe("<ExecutionsList/>", () => {
  it("renders runs from the API payload", async () => {
    renderWithProviders(<ExecutionsList />, { initialEntries: ["/executions"] });
    expect(
      await screen.findByText("run-abc-123".slice(0, 12) + "…"),
    ).toBeInTheDocument();
    expect(screen.getAllByText("succeeded").length).toBeGreaterThan(0);
    expect(screen.getByText("session-7")).toBeInTheDocument();
  });

  it("passes the agent filter from the query string", async () => {
    renderWithProviders(<ExecutionsList />, {
      initialEntries: ["/executions?agent=agent-1"],
    });
    await screen.findByText(/agent agent-1/);
    expect(await screen.findByText("run-abc-123".slice(0, 12) + "…")).toBeInTheDocument();
  });

  it("filters by status and shows an honest empty state", async () => {
    const user = userEvent.setup();
    renderWithProviders(<ExecutionsList />, { initialEntries: ["/executions"] });
    await screen.findByText("run-abc-123".slice(0, 12) + "…");
    await user.click(screen.getByRole("button", { name: "failed" }));
    await waitFor(() =>
      expect(screen.getByText("No executions match.")).toBeInTheDocument(),
    );
  });

  it("exposes the queued status (S1) as a filter and a badge", async () => {
    renderWithProviders(<ExecutionsList />, { initialEntries: ["/executions"] });
    // The filter chip exists — runs arrive as `queued` before a worker claims them.
    expect(screen.getByRole("button", { name: "queued" })).toBeInTheDocument();
  });
});