import { create } from "zustand";

import type { components } from "@/api/schema";

// Run-console projection (decision 4): a *pure* applyEvent reducer builds
// the timeline; text.delta buffers into `pendingText` and the store flushes
// it on a 50ms cycle (cleared on terminal/abort). Live SSE and JSON replay
// feed the same function → identical rendering.

/** The wire event: the generated envelope union (schema authority). */
export type WireEvent = components["schemas"]["CursorEvent"]["event"];

export type UsageView = components["schemas"]["Usage"];

export type RunStatus =
  | "idle"
  | "running"
  | "awaiting_input"
  | "completed"
  | "failed"
  | "cancelled";

/** S10: what the run needs from the human — a question to answer, or gated
 * tool calls to approve/refuse (both, for a batch that asks). */
export interface PendingCallView {
  id: string;
  name: string;
  arguments: Record<string, unknown>;
}

export interface PauseView {
  reason: "tool_approval" | "strategy";
  question: string | null;
  pendingCalls: PendingCallView[];
}

export interface ToolCardView {
  kind: "tool";
  id: string;
  toolCallId: string;
  name: string;
  status: "requested" | "running" | "completed" | "failed" | "declined";
  arguments?: Record<string, unknown>;
  output?: string;
  isError?: boolean;
  latencyMs?: number;
  error?: string;
  errorKind?: string;
}

export interface MessageView {
  kind: "message";
  id: string;
  text: string;
}

export interface IterationMarkerView {
  kind: "iteration";
  id: string;
  iteration: number;
}

export type TimelineItem =
  | ToolCardView
  | MessageView
  | IterationMarkerView;

export interface RunConsoleState {
  status: RunStatus;
  runId: string | null;
  sessionId: string | null;
  items: TimelineItem[];
  /** text.delta content not yet folded into a message item (50ms flush). */
  pendingText: string;
  /** Stream cursor (Last-Event-ID space) of the last frame applied. */
  lastEventId: number | null;
  finalMessage: string | null;
  error: { message: string; kind?: string } | null;
  usage: UsageView | null;
  iterations: number | null;
  /** S10: non-null while status is awaiting_input — the human's decision. */
  pause: PauseView | null;
  seenEventIds: ReadonlySet<string>;
}

export function initialRunConsoleState(): RunConsoleState {
  return {
    status: "idle",
    runId: null,
    sessionId: null,
    items: [],
    pendingText: "",
    lastEventId: null,
    finalMessage: null,
    error: null,
    usage: null,
    iterations: null,
    pause: null,
    seenEventIds: new Set(),
  };
}

/** Terminal/abort: pending buffer must never outlive the run. */
export function flushPending(state: RunConsoleState): RunConsoleState {
  if (state.pendingText === "") return state;
  return appendMessageText(
    { ...state, pendingText: "" },
    state.pendingText,
  );
}

/**
 * Pure projection. `cursor` is the SSE frame id (Last-Event-ID space); JSON
 * replay passes the CursorEvent cursor. Dedupes by event_id so reconnects
 * (which can replay the tail) never double-render.
 */
export function applyEvent(
  state: RunConsoleState,
  event: WireEvent,
  cursor: number | null,
): RunConsoleState {
  if (state.seenEventIds.has(event.event_id)) {
    return cursor !== null ? { ...state, lastEventId: cursor } : state;
  }
  const seenEventIds = new Set(state.seenEventIds);
  seenEventIds.add(event.event_id);
  const base: RunConsoleState = {
    ...state,
    seenEventIds,
    ...(cursor !== null ? { lastEventId: cursor } : {}),
    // S10: any event after a pause proves the run moved on — the answer was
    // consumed; terminal cases below override the status again.
    ...(state.status === "awaiting_input" ? { status: "running", pause: null } : {}),
  };

  switch (event.type) {
    case "run.started":
      return {
        ...base,
        status: "running",
        runId: event.run_id,
        sessionId: event.session_id ?? state.sessionId,
      };
    case "iteration.started": {
      const item: IterationMarkerView = {
        kind: "iteration",
        id: event.event_id,
        iteration: event.iteration,
      };
      return { ...base, items: [...state.items, item] };
    }
    case "iteration.completed":
      return base;
    case "run.awaiting_input": {
      // S10: the pause ends the segment — flush any pending assistant text,
      // surface the question/approval cards, and park until the human answers.
      const flushed = flushPending(base);
      const pause: PauseView = {
        reason: event.reason,
        question: event.question === "" ? null : event.question,
        pendingCalls: (event.pending_calls ?? []).map((call) => ({
          id: call.id,
          name: call.name,
          arguments: call.arguments ?? {},
        })),
      };
      return {
        ...flushed,
        status: "awaiting_input",
        pause,
        pendingText: "",
      };
    }
    case "model.invocation.started":
      return base;
    case "model.invocation.completed":
      return base;
    case "text.delta":
      // Buffered — folded into a message item by flushPending().
      return { ...base, pendingText: state.pendingText + event.text };
    case "tool.call.requested": {
      const item: ToolCardView = {
        kind: "tool",
        id: event.event_id,
        toolCallId: event.tool_call_id,
        name: event.name,
        status: "requested",
        arguments: event.arguments,
      };
      return { ...base, items: [...state.items, item] };
    }
    case "tool.call.started":
      return mapToolCard(base, event.tool_call_id, (card) => ({
        ...card,
        status: "running",
      }));
    case "tool.call.completed":
      return mapToolCard(base, event.tool_call_id, (card) => ({
        ...card,
        status: "completed",
        output: event.output,
        isError: event.is_error,
        latencyMs: event.latency_ms,
      }));
    case "tool.call.failed":
      return mapToolCard(base, event.tool_call_id, (card) => ({
        ...card,
        status: "failed",
        error: event.error,
        errorKind: event.kind,
      }));
    case "tool.call.declined":
      // ADR 0011 §3: the human's decision — the call never ran.
      return mapToolCard(base, event.tool_call_id, (card) => ({
        ...card,
        status: "declined",
      }));
    case "run.completed": {
      const flushed = flushPending(base);
      return {
        ...flushed,
        status: "completed",
        finalMessage: event.final_message,
        usage: event.total_usage,
        iterations: event.iterations,
        pendingText: "",
      };
    }
    case "run.failed": {
      const flushed = flushPending(base);
      return {
        ...flushed,
        status: "failed",
        error: {
          message: event.error,
          kind: event.error_kind,
        },
        usage: event.total_usage,
        pendingText: "",
      };
    }
    case "run.cancelled": {
      const flushed = flushPending(base);
      return {
        ...flushed,
        status: "cancelled",
        error: { message: event.reason, kind: "cancelled" },
        usage: event.total_usage,
        pendingText: "",
      };
    }
    default: {
      // Exhaustive: the union has no other members. The `never` check keeps
      // a future backend event type from silently rendering nothing — but at
      // runtime an unknown type still folds to base (forward-compat).
      const _exhaustive: never = event;
      void _exhaustive;
      return base;
    }
  }
}

function mapToolCard(
  state: RunConsoleState,
  toolCallId: string,
  update: (card: ToolCardView) => ToolCardView,
): RunConsoleState {
  return {
    ...state,
    items: state.items.map((item) =>
      item.kind === "tool" && item.toolCallId === toolCallId ? update(item) : item,
    ),
  };
}

function appendMessageText(state: RunConsoleState, text: string): RunConsoleState {
  const last = state.items[state.items.length - 1];
  if (last?.kind === "message") {
    return {
      ...state,
      items: [
        ...state.items.slice(0, -1),
        { ...last, text: last.text + text },
      ],
    };
  }
  return {
    ...state,
    items: [...state.items, { kind: "message", id: `msg-${state.items.length}`, text }],
  };
}

// --- live store -----------------------------------------------------------

interface RunConsoleStore extends RunConsoleState {
  apply: (event: WireEvent, cursor: number | null) => void;
  flush: () => void;
  /** Terminal/abort: fold pending text and stop the flush timer. */
  settle: () => void;
  reset: () => void;
}

const FLUSH_INTERVAL_MS = 50;
let flushTimer: ReturnType<typeof setInterval> | null = null;

function stopFlushTimer(): void {
  if (flushTimer !== null) {
    clearInterval(flushTimer);
    flushTimer = null;
  }
}

export const useRunConsoleStore = create<RunConsoleStore>((set) => ({
  ...initialRunConsoleState(),
  apply: (event, cursor) =>
    set((state) => {
      const next = applyEvent(state, event, cursor);
      // Start the coalescing cycle while deltas are pending.
      if (next.pendingText !== "" && flushTimer === null) {
        flushTimer = setInterval(() => useRunConsoleStore.getState().flush(), FLUSH_INTERVAL_MS);
      }
      return next;
    }),
  flush: () =>
    set((state) => {
      const next = flushPending(state);
      if (next.pendingText === "" && flushTimer !== null) {
        stopFlushTimer();
      }
      return next;
    }),
  settle: () =>
    set((state) => {
      stopFlushTimer();
      return flushPending(state);
    }),
  reset: () => {
    stopFlushTimer();
    set({ ...initialRunConsoleState() });
  },
}));