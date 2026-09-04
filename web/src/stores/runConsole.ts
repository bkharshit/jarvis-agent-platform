import { create } from "zustand";

import type { components } from "@/api/schema";

// Run-console projection (decision 4): a *pure* applyEvent reducer builds
// the timeline; text.delta buffers into `pendingText` and the store flushes
// it on a 50ms cycle (cleared on terminal/abort). Live SSE and JSON replay
// feed the same function → identical rendering.

/** The wire event: one of the envelope's discriminated types. */
export type WireEvent = {
  event_id: string;
  run_id: string;
  sequence: number | null;
  type: string;
} & Record<string, unknown>;

export type UsageView = components["schemas"]["Usage"];

export type RunStatus =
  | "idle"
  | "running"
  | "completed"
  | "failed"
  | "cancelled";

export interface ToolCardView {
  kind: "tool";
  id: string;
  toolCallId: string;
  name: string;
  status: "requested" | "running" | "completed" | "failed";
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
  };

  switch (event.type) {
    case "run.started":
      return {
        ...base,
        status: "running",
        runId: event.run_id,
        sessionId: (event.session_id as string | null) ?? state.sessionId,
      };
    case "iteration.started": {
      const item: IterationMarkerView = {
        kind: "iteration",
        id: event.event_id,
        iteration: event.iteration as number,
      };
      return { ...base, items: [...state.items, item] };
    }
    case "iteration.completed":
      return base;
    case "model.invocation.started":
      return base;
    case "model.invocation.completed":
      return base;
    case "text.delta":
      // Buffered — folded into a message item by flushPending().
      return { ...base, pendingText: state.pendingText + (event.text as string) };
    case "tool.call.requested": {
      const item: ToolCardView = {
        kind: "tool",
        id: event.event_id,
        toolCallId: event.tool_call_id as string,
        name: event.name as string,
        status: "requested",
        arguments: event.arguments as Record<string, unknown> | undefined,
      };
      return { ...base, items: [...state.items, item] };
    }
    case "tool.call.started":
      return mapToolCard(base, event.tool_call_id as string, (card) => ({
        ...card,
        status: "running",
      }));
    case "tool.call.completed":
      return mapToolCard(base, event.tool_call_id as string, (card) => ({
        ...card,
        status: "completed",
        output: event.output as string,
        isError: event.is_error as boolean,
        latencyMs: event.latency_ms as number,
      }));
    case "tool.call.failed":
      return mapToolCard(base, event.tool_call_id as string, (card) => ({
        ...card,
        status: "failed",
        error: event.error as string,
        errorKind: event.kind as string,
      }));
    case "run.completed": {
      const flushed = flushPending(base);
      return {
        ...flushed,
        status: "completed",
        finalMessage: event.final_message as string,
        usage: event.total_usage as UsageView,
        iterations: event.iterations as number,
        pendingText: "",
      };
    }
    case "run.failed": {
      const flushed = flushPending(base);
      return {
        ...flushed,
        status: "failed",
        error: {
          message: event.error as string,
          kind: event.error_kind as string,
        },
        usage: event.total_usage as UsageView,
        pendingText: "",
      };
    }
    case "run.cancelled": {
      const flushed = flushPending(base);
      return {
        ...flushed,
        status: "cancelled",
        error: { message: event.reason as string, kind: "cancelled" },
        usage: event.total_usage as UsageView,
        pendingText: "",
      };
    }
    default:
      // Unknown event types render nothing (forward-compat, like SectionGate).
      return base;
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