import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderWithProviders } from "@/test/render";
import { useRunConsoleStore } from "@/stores/runConsole";

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
  // The projection store is module-level — a prior test that ended mid-run
  // (status "running") would leave the Run button disabled for the next one.
  useRunConsoleStore.getState().reset();
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

  it("re-enables Run after a run completes so another run can start", async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve(
        sseResponse([frame("run.started", started), frame("run.completed", completed)]),
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
    await screen.findByTestId("final-message");

    // The button returns from "Running…" and a second run starts with no reload.
    const run = await screen.findByRole("button", { name: "Run" });
    await waitFor(() => expect(run).toBeEnabled());
    await user.click(run);
    expect(await screen.findByTestId("final-message")).toBeInTheDocument();
    expect(fetchMock.mock.calls.length).toBeGreaterThanOrEqual(2);
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

  it("shows approval cards on a pause and resumes with the approval (S10)", async () => {
    const pause = {
      event_id: "e2",
      run_id: "run-9",
      type: "run.awaiting_input",
      reason: "tool_approval",
      question: "",
      pending_calls: [{ id: "c1", name: "http_get", arguments: { url: "https://x" } }],
      awaiting_until: "2026-09-07T00:00:00Z",
    };
    const resumeRow = {
      run_id: "run-9",
      agent_id: "agent-1",
      status: "succeeded",
      input: "hi",
      agent_version_id: "v1",
    };
    const bodies: unknown[] = [];
    const fetchMock = vi.fn(async (...args: unknown[]) => {
      // The stream call is plain fetch(url, init); the resume POST goes
      // through openapi-fetch, which passes a Request whose body is on it.
      const [input, init] = args as [RequestInfo | URL, RequestInit | undefined];
      const url = input instanceof Request ? input.url : String(input);
      const method = input instanceof Request ? input.method : init?.method;
      if (method === "POST" && url.includes("/resume")) {
        const raw = input instanceof Request ? await input.text() : String(init?.body);
        bodies.push(JSON.parse(raw));
        return new Response(JSON.stringify(resumeRow), { status: 200 });
      }
      return sseResponse([
        frame("run.started", started),
        frame("run.awaiting_input", pause),
      ]);
    });
    vi.stubGlobal("fetch", fetchMock);

    const user = userEvent.setup();
    renderWithProviders(<RunConsole />, {
      initialEntries: ["/agents/agent-1/run"],
      path: "/agents/:agentId/run",
    });

    await user.type(await screen.findByLabelText("Run input"), "hi");
    await user.click(screen.getByRole("button", { name: "Run" }));

    const card = await screen.findByTestId("pause-card");
    expect(card).toHaveTextContent("http_get");
    expect(screen.getByTestId("pending-call-c1")).toBeInTheDocument();

    // ADR 0011: per-call selection staged locally, one Submit posts the map.
    await user.click(screen.getByTestId("allow-all"));
    await user.click(screen.getByTestId("submit-decisions"));

    await waitFor(() => {
      expect(bodies).toEqual([{ decisions: { c1: true } }]);
    });
    // the resume re-attaches the stream at the pause cursor
    await waitFor(() => {
      const init = fetchMock.mock.calls.at(-1)?.[1] as RequestInit | undefined;
      expect(init?.headers).toMatchObject({ "Last-Event-ID": "0" });
    });
  });

  it("stages per-call decisions and requires every call decided before submit (ADR 0011)", async () => {
    const pause = {
      event_id: "e2",
      run_id: "run-9",
      type: "run.awaiting_input",
      reason: "tool_approval",
      question: "",
      pending_calls: [
        { id: "c1", name: "calculator", arguments: { expression: "6*7" } },
        { id: "c2", name: "http_get", arguments: { url: "https://x" } },
      ],
      awaiting_until: "2026-09-07T00:00:00Z",
    };
    const bodies: unknown[] = [];
    const fetchMock = vi.fn(async (...args: unknown[]) => {
      const [input, init] = args as [RequestInfo | URL, RequestInit | undefined];
      const url = input instanceof Request ? input.url : String(input);
      const method = input instanceof Request ? input.method : init?.method;
      if (method === "POST" && url.includes("/resume")) {
        const raw = input instanceof Request ? await input.text() : String(init?.body);
        bodies.push(JSON.parse(raw));
        return new Response(JSON.stringify({ status: "succeeded", run_id: "run-9" }), {
          status: 200,
        });
      }
      return sseResponse([frame("run.started", started), frame("run.awaiting_input", pause)]);
    });
    vi.stubGlobal("fetch", fetchMock);

    const user = userEvent.setup();
    renderWithProviders(<RunConsole />, {
      initialEntries: ["/agents/agent-1/run"],
      path: "/agents/:agentId/run",
    });

    await user.type(await screen.findByLabelText("Run input"), "hi");
    await user.click(screen.getByRole("button", { name: "Run" }));
    await screen.findByTestId("pause-card");

    // undecided calls keep Submit disabled — silence never approves
    const submit = screen.getByTestId("submit-decisions");
    expect(submit).toBeDisabled();

    // approve the calculator, reject the http_get — per-call, staged
    await user.click(
      within(screen.getByTestId("pending-call-c1")).getByRole("button", { name: "Approve" }),
    );
    expect(submit).toBeDisabled(); // c2 still undecided
    await user.click(
      within(screen.getByTestId("pending-call-c2")).getByRole("button", { name: "Reject" }),
    );
    await user.click(submit);

    await waitFor(() => {
      expect(bodies).toEqual([{ decisions: { c1: true, c2: false } }]);
    });
  });

  it("collects the answer for a question pause and posts the content (S10)", async () => {
    const pause = {
      event_id: "e2",
      run_id: "run-9",
      type: "run.awaiting_input",
      reason: "strategy",
      question: "what is your name?",
      awaiting_until: "2026-09-07T00:00:00Z",
    };
    const bodies: unknown[] = [];
    const fetchMock = vi.fn(async (...args: unknown[]) => {
      // The stream call is plain fetch(url, init); the resume POST goes
      // through openapi-fetch, which passes a Request whose body is on it.
      const [input, init] = args as [RequestInfo | URL, RequestInit | undefined];
      const url = input instanceof Request ? input.url : String(input);
      const method = input instanceof Request ? input.method : init?.method;
      if (method === "POST" && url.includes("/resume")) {
        const raw = input instanceof Request ? await input.text() : String(init?.body);
        bodies.push(JSON.parse(raw));
        return Promise.resolve(
          new Response(
            JSON.stringify({
              run_id: "run-9",
              agent_id: "agent-1",
              status: "succeeded",
              input: "hi",
              agent_version_id: "v1",
            }),
            { status: 200 },
          ),
        );
      }
      return Promise.resolve(
        sseResponse([frame("run.started", started), frame("run.awaiting_input", pause)]),
      );
    });
    vi.stubGlobal("fetch", fetchMock);

    const user = userEvent.setup();
    renderWithProviders(<RunConsole />, {
      initialEntries: ["/agents/agent-1/run"],
      path: "/agents/:agentId/run",
    });

    await user.type(await screen.findByLabelText("Run input"), "hi");
    await user.click(screen.getByRole("button", { name: "Run" }));

    expect(await screen.findByTestId("pause-card")).toHaveTextContent("what is your name?");
    await user.type(await screen.findByLabelText("Your answer"), "Harshit");
    await user.click(screen.getByRole("button", { name: "Send answer" }));

    await waitFor(() => {
      expect(bodies).toEqual([{ content: "Harshit" }]);
    });
  });
});