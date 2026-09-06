import { describe, expect, it } from "vitest";

import {
  applyEvent,
  flushPending,
  initialRunConsoleState,
  type RunConsoleState,
  type WireEvent,
} from "./runConsole";

let counter = 0;
function ev(fields: Record<string, unknown>): WireEvent {
  counter += 1;
  return {
    event_id: fields.event_id as string ?? `e${counter}`,
    run_id: (fields.run_id as string) ?? "run-1",
    sequence: (fields.sequence as number) ?? counter,
    ...fields,
  } as WireEvent;
}

function started(): WireEvent {
  return ev({
    type: "run.started",
    agent_id: "agent-1",
    agent_version_id: "v1",
    session_id: "s1",
    input: "hi",
  });
}

function apply(state: RunConsoleState, ...events: WireEvent[]): RunConsoleState {
  return events.reduce(
    (acc, event) => applyEvent(acc, event, 1),
    state,
  );
}

describe("applyEvent", () => {
  it("builds the timeline for a full successful run", () => {
    const state = apply(
      initialRunConsoleState(),
      started(),
      ev({ type: "iteration.started", iteration: 1 }),
      ev({ type: "tool.call.requested", tool_call_id: "t1", name: "calculator", arguments: { x: 1 } }),
      ev({ type: "tool.call.started", tool_call_id: "t1", name: "calculator" }),
      ev({
        type: "tool.call.completed",
        tool_call_id: "t1",
        name: "calculator",
        output: "42",
        is_error: false,
        latency_ms: 12,
      }),
      ev({ type: "text.delta", text: "The answer is " }),
      ev({ type: "text.delta", text: "42" }),
      ev({
        type: "run.completed",
        final_message: "The answer is 42",
        total_usage: { input_tokens: 10, output_tokens: 5 },
        iterations: 1,
      }),
    );

    expect(state.status).toBe("completed");
    expect(state.runId).toBe("run-1");
    expect(state.sessionId).toBe("s1");
    expect(state.finalMessage).toBe("The answer is 42");
    expect(state.pendingText).toBe("");

    const kinds = state.items.map((i) => i.kind);
    expect(kinds).toEqual(["iteration", "tool", "message"]);
    const tool = state.items[1] as Extract<typeof state.items[number], { kind: "tool" }>;
    expect(tool.status).toBe("completed");
    expect(tool.output).toBe("42");
    const message = state.items[2] as Extract<typeof state.items[number], { kind: "message" }>;
    expect(message.text).toBe("The answer is 42");
  });

  it("buffers text.delta in pendingText until flushed", () => {
    const state = apply(
      initialRunConsoleState(),
      started(),
      ev({ type: "text.delta", text: "hel" }),
      ev({ type: "text.delta", text: "lo" }),
    );
    expect(state.pendingText).toBe("hello");
    expect(state.items).toHaveLength(0);

    const flushed = flushPending(state);
    expect(flushed.pendingText).toBe("");
    expect(flushed.items[0]).toMatchObject({ kind: "message", text: "hello" });
  });

  it("appends flushed text to the last message item", () => {
    const one = flushPending(
      apply(initialRunConsoleState(), started(), ev({ type: "text.delta", text: "a" })),
    );
    const two = flushPending(
      apply(one, ev({ type: "text.delta", text: "b" })),
    );
    expect(two.items).toHaveLength(1);
    expect((two.items[0] as { text: string }).text).toBe("ab");
  });

  it("folds pending text into terminal events (never lost)", () => {
    const state = apply(
      initialRunConsoleState(),
      started(),
      ev({ type: "text.delta", text: "partial" }),
      ev({ type: "run.failed", error: "boom", error_kind: "model", total_usage: { input_tokens: 1, output_tokens: 1 } }),
    );
    expect(state.pendingText).toBe("");
    expect(state.status).toBe("failed");
    expect(state.error).toEqual({ message: "boom", kind: "model" });
    // The partial text survives as a message item.
    expect(state.items.some((i) => i.kind === "message" && i.text === "partial")).toBe(true);
  });

  it("tracks tool-card state through its lifecycle", () => {
    const requested = apply(
      initialRunConsoleState(),
      started(),
      ev({ type: "tool.call.requested", tool_call_id: "t1", name: "calc", arguments: {} }),
    );
    const card = (s: RunConsoleState) =>
      s.items.find((i) => i.kind === "tool") as Extract<RunConsoleState["items"][number], { kind: "tool" }>;
    expect(card(requested).status).toBe("requested");

    const running = apply(requested, ev({ type: "tool.call.started", tool_call_id: "t1", name: "calc" }));
    expect(card(running).status).toBe("running");

    const failed = apply(
      running,
      ev({ type: "tool.call.failed", tool_call_id: "t1", name: "calc", error: "bad input", kind: "validation" }),
    );
    expect(card(failed).status).toBe("failed");
    expect(card(failed).error).toBe("bad input");
    expect(card(failed).errorKind).toBe("validation");
  });

  it("dedupes by event_id (reconnect tail replay)", () => {
    const delta = ev({ type: "text.delta", text: "once" });
    const once = apply(initialRunConsoleState(), started(), delta);
    const twice = applyEvent(once, delta, 1);
    expect(twice.pendingText).toBe("once");
  });

  it("ignores unknown event types (forward-compat)", () => {
    const state = apply(initialRunConsoleState(), started(), ev({ type: "run.mystery", foo: 1 }));
    expect(state.items).toHaveLength(0);
    expect(state.seenEventIds.size).toBe(2);
  });

  it("folds all three terminals", () => {
    const cancelled = apply(
      initialRunConsoleState(),
      started(),
      ev({ type: "run.cancelled", reason: "user requested", total_usage: { input_tokens: 0, output_tokens: 0 } }),
    );
    expect(cancelled.status).toBe("cancelled");
    expect(cancelled.error?.message).toBe("user requested");
  });

  it("surfaces the pause view on run.awaiting_input (S10)", () => {
    const paused = apply(
      initialRunConsoleState(),
      started(),
      ev({ type: "text.delta", text: "let me ask first…" }),
      ev({
        type: "run.awaiting_input",
        reason: "strategy",
        question: "what is your name?",
        awaiting_until: "2026-09-07T00:00:00Z",
      }),
    );
    expect(paused.status).toBe("awaiting_input");
    expect(paused.pause).toEqual({
      reason: "strategy",
      question: "what is your name?",
      pendingCalls: [],
    });
    // pending assistant text is folded into the timeline, never lost
    expect(paused.pendingText).toBe("");
    expect(paused.items.some((i) => i.kind === "message" && i.text === "let me ask first…")).toBe(true);
  });

  it("carries gated tool calls in the pause view", () => {
    const paused = apply(
      initialRunConsoleState(),
      started(),
      ev({ type: "tool.call.requested", tool_call_id: "t1", name: "http_get", arguments: { url: "https://x" } }),
      ev({
        type: "run.awaiting_input",
        reason: "tool_approval",
        question: "",
        pending_calls: [{ id: "t1", name: "http_get", arguments: { url: "https://x" } }],
        awaiting_until: "2026-09-07T00:00:00Z",
      }),
    );
    expect(paused.status).toBe("awaiting_input");
    expect(paused.pause).toEqual({
      reason: "tool_approval",
      question: null, // empty question renders as none
      pendingCalls: [{ id: "t1", name: "http_get", arguments: { url: "https://x" } }],
    });
  });

  it("clears the pause when the resumed segment's events arrive", () => {
    const paused = apply(
      initialRunConsoleState(),
      started(),
      ev({ type: "run.awaiting_input", reason: "strategy", question: "q?", awaiting_until: "2026-09-07T00:00:00Z" }),
    );
    const resumed = apply(paused, ev({ type: "iteration.started", iteration: 2 }));
    expect(resumed.status).toBe("running");
    expect(resumed.pause).toBeNull();

    // …and a second pause parks the run again
    const pausedAgain = apply(
      resumed,
      ev({ type: "run.awaiting_input", reason: "tool_approval", question: "", pending_calls: [], awaiting_until: "2026-09-07T00:00:00Z" }),
    );
    expect(pausedAgain.status).toBe("awaiting_input");

    // …and the terminal wins over a stale awaiting_input state
    const done = apply(
      pausedAgain,
      ev({ type: "run.completed", final_message: "done", total_usage: { input_tokens: 1, output_tokens: 1 }, iterations: 1 }),
    );
    expect(done.status).toBe("completed");
  });
});