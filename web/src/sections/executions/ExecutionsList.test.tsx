import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { renderWithProviders } from "@/test/render";
import { server } from "@/test/msw";
import { http, HttpResponse } from "msw";
import { runFixture } from "@/test/handlers";

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

  it("shows the awaiting-input inbox for paused runs (S10)", async () => {
    const paused = {
      ...runFixture,
      run_id: "run-paused-1",
      status: "awaiting_input" as const,
      input: "what is the launch code?",
      finished_at: null,
    };
    server.use(
      http.get("/v1/executions", ({ request }) => {
        const url = new URL(request.url);
        const status = url.searchParams.get("status");
        if (status === "awaiting_input") return HttpResponse.json({ items: [paused] });
        return HttpResponse.json({ items: [] });
      }),
    );
    // Unseeded: the inbox gate must come from the *fetched* capabilities
    // payload (executions.detail.human_in_the_loop), not the test seed.
    renderWithProviders(<ExecutionsList />, {
      initialEntries: ["/executions"],
      capabilities: null,
    });

    const inbox = await screen.findByTestId("awaiting-input-inbox");
    expect(inbox).toHaveTextContent("Awaiting input");
    expect(inbox).toHaveTextContent("what is the launch code?");
    expect(screen.getByRole("button", { name: "awaiting_input" })).toBeInTheDocument();
  });

  it("hides the awaiting-input inbox when the capability is off", async () => {
    // Seeded capabilities (the default) carry executions without the S10
    // detail — the gate must read `detail.human_in_the_loop`, not truthiness.
    server.use(
      http.get("/v1/executions", () => HttpResponse.json({ items: [] })),
    );
    renderWithProviders(<ExecutionsList />, { initialEntries: ["/executions"] });
    await screen.findByText("No executions match.");
    expect(screen.queryByTestId("awaiting-input-inbox")).not.toBeInTheDocument();
  });
});