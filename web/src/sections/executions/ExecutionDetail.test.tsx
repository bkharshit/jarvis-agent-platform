import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import { eventLogFixture, runFixture } from "@/test/handlers";
import { server } from "@/test/msw";
import { TEST_CAPABILITIES, renderWithProviders } from "@/test/render";

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

  it("renders the pause card for an awaiting_input run and resumes it (S10)", async () => {
    // A durable pause: detail row awaiting_input, event log ending in the
    // pause frame. The resume POST flips the row the refetch will see.
    const pausedRow = {
      ...runFixture,
      run_id: "run-paused",
      status: "awaiting_input" as const,
      final_message: null,
    };
    const pauseLog = [
      ...eventLogFixture.slice(0, 2),
      {
        cursor: 2,
        event: {
          event_id: "e-pause",
          run_id: "run-paused",
          type: "run.awaiting_input",
          reason: "tool_approval",
          question: "",
          pending_calls: [{ id: "c9", name: "calculator", arguments: { expression: "22/7" } }],
          awaiting_until: "2026-09-08T00:00:00Z",
        },
      },
    ];
    let status: string = "awaiting_input";
    const bodies: unknown[] = [];
    server.use(
      http.get("/v1/executions/run-paused", () =>
        HttpResponse.json({ run: { ...pausedRow, status }, messages: [], tool_executions: [] }),
      ),
      http.get("/v1/executions/run-paused/events", () =>
        HttpResponse.json({ run_id: "run-paused", after: null, events: pauseLog }),
      ),
      http.post("/v1/executions/run-paused/resume", async ({ request }) => {
        bodies.push(await request.json());
        status = "succeeded";
        return HttpResponse.json({ ...pausedRow, status });
      }),
    );

    const user = userEvent.setup();
    renderWithProviders(<ExecutionDetailPage />, {
      initialEntries: ["/executions/run-paused"],
      path: "/executions/:runId",
    });

    // the pause frame's pending call drives the shared PauseCard
    const card = await screen.findByTestId("pause-card");
    expect(card).toHaveTextContent("calculator");
    expect(screen.getByTestId("pending-call-c9")).toBeInTheDocument();

    // staged per-call decision, then submit — silence never approves (D33)
    expect(screen.getByTestId("submit-decisions")).toBeDisabled();
    await user.click(screen.getByTestId("allow-all"));
    await user.click(screen.getByTestId("submit-decisions"));

    await waitFor(() => {
      expect(bodies).toEqual([{ decisions: { c9: true } }]);
    });
    // the refetched row left awaiting_input — the card goes with it
    await waitFor(() => {
      expect(screen.queryByTestId("pause-card")).not.toBeInTheDocument();
    });
  });

  it("offers cancel on an awaiting_input run and posts it", async () => {
    const pausedRow = {
      ...runFixture,
      run_id: "run-paused-2",
      status: "awaiting_input" as const,
      final_message: null,
    };
    const bodies: unknown[] = [];
    server.use(
      http.get("/v1/executions/run-paused-2", () =>
        HttpResponse.json({
          run: pausedRow,
          messages: [],
          tool_executions: [],
        }),
      ),
      http.get("/v1/executions/run-paused-2/events", () =>
        HttpResponse.json({
          run_id: "run-paused-2",
          after: null,
          events: [
            {
              cursor: 0,
              event: {
                event_id: "e-pause-2",
                run_id: "run-paused-2",
                type: "run.awaiting_input",
                reason: "strategy",
                question: "what is your name?",
                pending_calls: [],
                awaiting_until: "2026-09-08T00:00:00Z",
              },
            },
          ],
        }),
      ),
      http.post("/v1/executions/run-paused-2/cancel", () => {
        bodies.push("cancelled"); // the cancel POST carries no body
        return HttpResponse.json({
          run_id: "run-paused-2",
          cancelled: true,
          status: "cancelled",
        });
      }),
    );

    const user = userEvent.setup();
    renderWithProviders(<ExecutionDetailPage />, {
      initialEntries: ["/executions/run-paused-2"],
      path: "/executions/:runId",
    });

    // a question pause shows the answer form
    expect(await screen.findByTestId("pause-card")).toHaveTextContent("what is your name?");
    await user.click(await screen.findByTestId("cancel-run"));
    await waitFor(() => {
      expect(bodies).toHaveLength(1);
    });
  });
});

describe("LLM trace section (ADR 0014)", () => {
  /** The debug flag is a capabilities detail fact — the seeded default has
   * no executions detail, so the section stays hidden unless overridden. */
  const traceOn = {
    ...TEST_CAPABILITIES,
    sections: {
      ...TEST_CAPABILITIES.sections,
      executions: {
        enabled: true,
        summary: "Browse runs",
        detail: { human_in_the_loop: true, llm_trace: true },
      },
    },
  };

  it("is hidden when the llm_trace capability flag is off", async () => {
    renderDetail();
    expect(await screen.findByText("run-abc-123")).toBeInTheDocument();
    expect(screen.queryByTestId("llm-trace")).not.toBeInTheDocument();
  });

  it("lists entries and shows the request payload on expand", async () => {
    server.use(
      http.get("/v1/executions/run-abc-123/llm-trace", () =>
        HttpResponse.json({
          run_id: "run-abc-123",
          entries: [
            {
              iteration: 0,
              method: "stream",
              provider: "mock",
              model: "mock-model",
              at: "2026-09-12T00:00:00Z",
              request: { model: "mock-model", messages: [{ role: "system", content: "You are a test agent." }] },
              response: { message: { role: "assistant", content: "It is 4." }, finish_reason: "stop" },
            },
          ],
        }),
      ),
    );
    const user = userEvent.setup();
    renderWithProviders(<ExecutionDetailPage />, {
      initialEntries: ["/executions/run-abc-123"],
      path: "/executions/:runId",
      capabilities: traceOn,
    });

    const summary = await screen.findByText(/iteration 0 · mock\/mock-model · stream/);
    expect(screen.getByTestId("llm-trace")).toHaveTextContent(
      "iteration 0 · mock/mock-model · stream",
    );
    // the payload is behind <details> — expand to see the ACTUAL request
    await user.click(summary);
    expect(await screen.findByText(/You are a test agent/)).toBeInTheDocument();
  });

  it("explains an empty trace instead of erroring", async () => {
    server.use(
      http.get("/v1/executions/run-abc-123/llm-trace", () =>
        HttpResponse.json({ run_id: "run-abc-123", entries: [] }),
      ),
    );
    renderWithProviders(<ExecutionDetailPage />, {
      initialEntries: ["/executions/run-abc-123"],
      path: "/executions/:runId",
      capabilities: traceOn,
    });

    expect(await screen.findByText(/No trace recorded/)).toBeInTheDocument();
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