import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { eventLogFixture, runFixture } from "@/test/handlers";
import { renderWithProviders } from "@/test/render";

import { projectReplay } from "./ExecutionDetail";
import { applyEvent, flushPending, initialRunConsoleState } from "@/stores/runConsole";
import type { WireEvent } from "@/stores/runConsole";

import { ExecutionDetailPage } from "./ExecutionDetail";

function renderDetail() {
  return renderWithProviders(<ExecutionDetailPage />, {
    initialEntries: ["/executions/run-abc-123"],
    path: "/executions/:runId",
  });
}

describe("<ExecutionDetailPage/>", () => {
  it("renders the run summary, transcript, and tool executions", async () => {
    renderDetail();
    expect(await screen.findByText("run-abc-123")).toBeInTheDocument();
    // Final message + transcript row agree (fixture has both).
    expect(screen.getAllByText("It is 4.").length).toBe(2);
    expect(screen.getByText("12 in / 4 out · 1 iterations")).toBeInTheDocument();
    expect(screen.getByText("calculator")).toBeInTheDocument();
    expect(screen.getByText("4")).toBeInTheDocument(); // ToolResult.output top-level (D17)
  });

  it("replays the event log through the shared reducer", async () => {
    const user = userEvent.setup();
    renderDetail();

    await user.click(await screen.findByRole("button", { name: "Replay events" }));
    const timeline = await screen.findByTestId("replay-timeline");
    expect(timeline).toHaveTextContent("It is 4."); // flushed text.delta
    expect(timeline).toHaveTextContent("calculator");
    expect(timeline).toHaveTextContent("iteration 1");
    expect(screen.getByRole("button", { name: "Hide replay" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  it("shows an honest not-found state", async () => {
    renderWithProviders(<ExecutionDetailPage />, {
      initialEntries: ["/executions/nope"],
      path: "/executions/:runId",
    });
    expect(await screen.findByText("execution nope not found")).toBeInTheDocument();
  });
});

describe("replay ≡ live (decision 4)", () => {
  it("the JSON replay projection equals the live SSE projection", () => {
    // Live path: every event streamed through applyEvent (deltas coalesced,
    // then flushed at terminal) — exactly what the run console renders.
    let live = initialRunConsoleState();
    for (const pair of eventLogFixture) {
      live = applyEvent(live, pair.event as WireEvent, pair.cursor);
    }
    live = flushPending(live);

    // Replay path: projectReplay over the same log.
    const replayed = projectReplay(eventLogFixture.map((p) => p.event as WireEvent));

    expect(replayed).toEqual(live.items);
  });

  it("renders the fixture's final message in both paths", () => {
    const items = projectReplay(eventLogFixture.map((p) => p.event as WireEvent));
    const messages = items.filter((i) => i.kind === "message");
    expect(messages).toHaveLength(1);
    expect((messages[0] as { text: string }).text).toBe("It is 4.");
    // runFixture's own final message matches — the projection agrees with the row.
    expect(runFixture.final_message).toBe("It is 4.");
  });
});