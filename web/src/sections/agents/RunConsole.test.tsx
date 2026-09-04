import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderWithProviders } from "@/test/render";

import { RunConsole } from "./RunConsole";

// The SSE layer is stubbed at fetch with a hand-built ReadableStream
// (decision 12) — msw is not involved in streaming.

function sseResponse(frames: string[]): Response {
  const encoder = new TextEncoder();
  let index = 0;
  const stream = new ReadableStream<Uint8Array>({
    pull(controller) {
      if (index < frames.length) {
        controller.enqueue(encoder.encode(frames[index++]));
      } else {
        controller.close();
      }
    },
  });
  return new Response(stream, {
    status: 200,
    headers: { "Content-Type": "text/event-stream" },
  });
}

function frame(event: string, data: object): string {
  return `id: 0\nevent: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
}

const started = {
  event_id: "e0",
  run_id: "run-9",
  type: "run.started",
  agent_id: "agent-1",
  agent_version_id: "v1",
  session_id: null,
  input: "hello",
};

const completed = {
  event_id: "e1",
  run_id: "run-9",
  type: "run.completed",
  final_message: "All done",
  total_usage: { input_tokens: 3, output_tokens: 2 },
  iterations: 1,
};

const cancelRoute = vi.fn(
  () => new Response(JSON.stringify({ run_id: "run-9", cancelled: true, status: "running" })),
);

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("<RunConsole/>", () => {
  it("streams a run to the final message and shows usage", async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve(
        sseResponse([
          frame("run.started", started),
          frame("text.delta", { event_id: "e0.5", run_id: "run-9", type: "text.delta", text: "working…" }),
          frame("run.completed", completed),
        ]),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    const user = userEvent.setup();
    renderWithProviders(<RunConsole />, {
      initialEntries: ["/agents/agent-1/run"],
      path: "/agents/:agentId/run",
    });

    await user.type(await screen.findByLabelText("Run input"), "hello");
    await user.click(screen.getByRole("button", { name: "Run" }));

    expect(
      await screen.findByTestId("final-message"),
    ).toHaveTextContent("All done");
    expect(screen.getByText(/3 in \/ 2 out/)).toBeInTheDocument();
  });

  it("sends the cancel request against the live run", async () => {
    const fetchMock = vi.fn((...args: unknown[]) => {
      const [input] = args as [RequestInfo | URL];
      const url = input instanceof Request ? input.url : String(input);
      if (url.includes("/cancel")) return Promise.resolve(cancelRoute());
      return Promise.resolve(sseResponse([frame("run.started", started)]));
    });
    vi.stubGlobal("fetch", fetchMock);

    const user = userEvent.setup();
    renderWithProviders(<RunConsole />, {
      initialEntries: ["/agents/agent-1/run"],
      path: "/agents/:agentId/run",
    });

    await user.type(await screen.findByLabelText("Run input"), "hello");
    await user.click(screen.getByRole("button", { name: "Run" }));
    const cancel = await screen.findByRole("button", { name: "Cancel" });
    await user.click(cancel);
    await waitFor(() => {
      const last = fetchMock.mock.calls.at(-1)?.[0];
      const url = last instanceof Request ? last.url : String(last);
      expect(url).toContain("/v1/executions/run-9/cancel");
    });
  });
});