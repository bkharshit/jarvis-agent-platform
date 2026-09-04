import { afterEach, describe, expect, it, vi } from "vitest";

import { FrameParser, connectRunStream, type SseFrame } from "./sse";

function frame(id: number, event: string, data: object): string {
  return `id: ${id}\nevent: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
}

function sseResponse(chunks: string[]): Response {
  const encoder = new TextEncoder();
  let index = 0;
  const stream = new ReadableStream<Uint8Array>({
    pull(controller) {
      if (index < chunks.length) {
        controller.enqueue(encoder.encode(chunks[index++]));
      } else {
        controller.close();
      }
    },
  });
  return new Response(stream, { status: 200, headers: { "Content-Type": "text/event-stream" } });
}

describe("FrameParser", () => {
  it("parses complete frames from a single chunk", () => {
    const parser = new FrameParser();
    const frames = parser.parse(frame(3, "run.started", { event_id: "e1" }));
    expect(frames).toHaveLength(1);
    expect(frames[0].id).toBe(3);
    expect(frames[0].event).toBe("run.started");
    expect(JSON.parse(frames[0].data)).toEqual({ event_id: "e1" });
  });

  it("buffers a frame split across chunks", () => {
    const parser = new FrameParser();
    expect(parser.parse('id: 1\nevent: text.del')).toEqual([]);
    expect(parser.parse('ta\ndata: {"text":"hi"}\n\n')).toHaveLength(1);
  });

  it("ignores keep-alive comment frames", () => {
    const parser = new FrameParser();
    expect(parser.parse(": keep-alive\n\n")).toEqual([]);
  });

  it("joins multi-line data and tracks the last id", () => {
    const parser = new FrameParser();
    const frames = parser.parse(
      "id: 5\nevent: message\ndata: line1\ndata: line2\n\n",
    );
    expect(frames[0].data).toBe("line1\nline2");
    // A frame without an id inherits the last seen id (SSE semantics).
    const [noId] = parser.parse("event: ping\ndata: {}\n\n");
    expect(noId?.id).toBe(5);
  });
});

// --- connectRunStream ------------------------------------------------------

type FetchCall = { url: string; headers: Record<string, string>; body: unknown };

function stubFetch(respond: (callIndex: number) => Response) {
  const calls: FetchCall[] = [];
  const fetchMock = vi.fn(
    (...args: [RequestInfo | URL, RequestInit?]) => {
      const [input, init] = args;
      calls.push({
        url: String(input),
        headers: Object.fromEntries(new Headers(init?.headers)),
        body: init?.body ? JSON.parse(init.body as string) : null,
      });
      // A fresh Response per call — a body can only be read once.
      return Promise.resolve(respond(calls.length - 1));
    },
  );
  vi.stubGlobal("fetch", fetchMock);
  return { calls, fetchMock };
}

function firstCall(...responses: Response[]): (callIndex: number) => Response {
  return (callIndex) => responses[Math.min(callIndex, responses.length - 1)];
}

const START = { event_id: "e0", run_id: "run-1", type: "run.started" };
const DONE = {
  event_id: "e1",
  run_id: "run-1",
  type: "run.completed",
  final_message: "done",
};

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("connectRunStream", () => {
  it("delivers frames and reports finished on a terminal event", async () => {
    const { calls } = stubFetch(firstCall(
      sseResponse([frame(0, "run.started", START), frame(1, "run.completed", DONE)]),
    ));
    const frames: SseFrame[] = [];
    const statuses: string[] = [];
    connectRunStream("agent-1", {
      body: { input: "hi" },
      getLastEventId: () => null,
      getRunId: () => null,
      onFrame: (f) => frames.push(f),
      onStatus: (s) => statuses.push(s.status),
    });
    await vi.waitFor(() => expect(statuses).toContain("finished"));
    expect(frames).toHaveLength(2);
    expect(calls[0].url).toBe("/v1/agents/agent-1/stream");
  });

  it("reconnects with run_id + Last-Event-ID when the stream ends early", async () => {
    const { calls } = stubFetch(firstCall(
      sseResponse([frame(0, "run.started", START)]), // ends without terminal
      sseResponse([frame(1, "run.completed", DONE)]), // reconnect delivers terminal
    ));
    const statuses: string[] = [];
    connectRunStream("agent-1", {
      body: { input: "hi" },
      getLastEventId: () => 7,
      getRunId: () => "run-1",
      onFrame: () => {},
      onStatus: (s) => statuses.push(s.status),
    });
    await vi.waitFor(() => expect(statuses).toContain("finished"));
    await vi.waitFor(() => expect(calls.length).toBeGreaterThanOrEqual(2));
    expect(calls[1].headers["last-event-id"]).toBe("7");
    expect(calls[1].body).toMatchObject({ run_id: "run-1", input: "hi" });
    expect(statuses).toContain("reconnecting");
  });

  it("parks in disconnected after 3 failed reconnects", { timeout: 10000 }, async () => {
    const { calls } = stubFetch(() =>
      sseResponse([frame(0, "run.started", START)]),
    );
    const statuses: string[] = [];
    connectRunStream("agent-1", {
      body: { input: "hi" },
      getLastEventId: () => null,
      getRunId: () => null,
      onFrame: () => {},
      onStatus: (s) => statuses.push(s.status),
    });
    await vi.waitFor(() => expect(statuses).toContain("disconnected"), {
      timeout: 10000,
    });
    expect(calls).toHaveLength(4); // 1 + 3 reconnects
  });

  it("reports the envelope message on an error response", async () => {
    stubFetch(() =>
      new Response(
        JSON.stringify({ error: { kind: "not_found", message: "no such agent" } }),
        { status: 404 },
      ),
    );
    const statuses: string[] = [];
    connectRunStream("agent-1", {
      body: { input: "hi" },
      getLastEventId: () => null,
      getRunId: () => null,
      onFrame: () => {},
      onStatus: (s) => statuses.push(JSON.stringify(s)),
    });
    await vi.waitFor(() => expect(statuses.join("")).toContain("no such agent"));
  });
});