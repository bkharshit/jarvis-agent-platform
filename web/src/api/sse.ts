// SSE client (decision 3). POST /stream cannot use EventSource, so frames
// come off a fetch ReadableStream. Wire format (ADR 0003): one frame per
// event — `id: <durable cursor>`, `event: <type>`, `data: <event JSON>` —
// plus `: keep-alive` comment frames. A stream end without a terminal event
// is NOT completion: reconnect 3× (300ms/1s/3s), then park in `disconnected`
// and let the user Resume (the server replays from the DB).

export interface SseFrame {
  /** Durable cursor (Last-Event-ID space), when the frame carries an id. */
  id: number | null;
  event: string;
  data: string;
}

/** Incremental frame parser: feed decoded chunks, get complete frames. */
export class FrameParser {
  private buffer = "";
  private lastId: number | null = null;

  parse(chunk: string): SseFrame[] {
    this.buffer += chunk;
    const frames: SseFrame[] = [];
    let boundary = this.buffer.indexOf("\n\n");
    while (boundary !== -1) {
      const raw = this.buffer.slice(0, boundary);
      this.buffer = this.buffer.slice(boundary + 2);
      const frame = this.toFrame(raw);
      if (frame) frames.push(frame);
      boundary = this.buffer.indexOf("\n\n");
    }
    return frames;
  }

  private toFrame(raw: string): SseFrame | null {
    const dataLines: string[] = [];
    let event = "message";
    let id: number | null = this.lastId;
    for (const line of raw.split("\n")) {
      if (line.startsWith(":")) continue; // comment (keep-alive)
      if (line.startsWith("data:")) {
        dataLines.push(line.slice(5).trimStart());
      } else if (line.startsWith("event:")) {
        event = line.slice(6).trim();
      } else if (line.startsWith("id:")) {
        const parsed = Number(line.slice(3).trim());
        if (!Number.isNaN(parsed)) id = parsed;
      }
    }
    if (dataLines.length === 0) return null;
    if (id !== null) this.lastId = id;
    return { id, event, data: dataLines.join("\n") };
  }
}

export type StreamStatus =
  | { status: "connecting" }
  | { status: "live" }
  | { status: "reconnecting"; attempt: number } // 1-based
  | { status: "disconnected" }
  | { status: "finished" }
  | { status: "error"; message: string };

export const TERMINAL_EVENT_TYPES = new Set([
  "run.completed",
  "run.failed",
  "run.cancelled",
]);

const RECONNECT_DELAYS_MS = [300, 1000, 3000];

export interface RunStreamOptions {
  /** Body for a fresh run; re-POSTed on reconnect (resume uses run_id). */
  body: { input: string; run_id?: string | null; session_id?: string | null };
  /** Called before every (re)connect — the current resume cursor. */
  getLastEventId: () => number | null;
  /** Called on every (re)connect — the run to attach to (null = fresh). */
  getRunId: () => string | null;
  onFrame: (frame: SseFrame) => void;
  onStatus: (status: StreamStatus) => void;
}

export interface RunStreamHandle {
  abort: () => void;
}

export function connectRunStream(
  agentId: string,
  options: RunStreamOptions,
): RunStreamHandle {
  const controller = new AbortController();
  void runLoop(agentId, options, controller);
  return { abort: () => controller.abort() };
}

async function runLoop(
  agentId: string,
  options: RunStreamOptions,
  controller: AbortController,
): Promise<void> {
  for (let attempt = 0; attempt <= RECONNECT_DELAYS_MS.length; attempt++) {
    if (controller.signal.aborted) return;
    if (attempt > 0) {
      options.onStatus({ status: "reconnecting", attempt });
      await sleep(RECONNECT_DELAYS_MS[attempt - 1], controller.signal);
      if (controller.signal.aborted) return;
    }
    try {
      const done = await connectOnce(agentId, options, controller.signal);
      if (done) {
        options.onStatus({ status: "finished" });
        return;
      }
      // Stream closed without a terminal event.
    } catch (err) {
      if (controller.signal.aborted) return;
      options.onStatus({
        status: "error",
        message: err instanceof Error ? err.message : "stream failed",
      });
      return;
    }
  }
  // All reconnect attempts burned — park with a Resume button upstream.
  options.onStatus({ status: "disconnected" });
}

async function connectOnce(
  agentId: string,
  options: RunStreamOptions,
  signal: AbortSignal,
): Promise<boolean> {
  const runId = options.getRunId();
  const lastEventId = options.getLastEventId();
  const response = await fetch(`/v1/agents/${agentId}/stream`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: "text/event-stream",
      ...(lastEventId !== null ? { "Last-Event-ID": String(lastEventId) } : {}),
    },
    body: JSON.stringify({
      input: options.body.input,
      ...(runId !== null ? { run_id: runId } : {}),
      ...(options.body.session_id ? { session_id: options.body.session_id } : {}),
    }),
    signal,
  });
  if (!response.ok || !response.body) {
    let message = `stream failed with status ${response.status}`;
    try {
      const body = (await response.json()) as {
        error?: { kind?: string; message?: string };
      };
      if (body.error?.message) message = body.error.message;
    } catch {
      // non-JSON — keep the generic message
    }
    throw new Error(message);
  }

  options.onStatus({ status: "live" });
  const parser = new FrameParser();
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let terminal = false;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    for (const frame of parser.parse(decoder.decode(value, { stream: true }))) {
      options.onFrame(frame);
      if (TERMINAL_EVENT_TYPES.has(frame.event)) terminal = true;
    }
  }
  return terminal;
}

function sleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const timer = setTimeout(resolve, ms);
    // Resolve (not reject) on abort — runLoop re-checks the signal, so an
    // abort during the backoff never becomes an unhandled rejection.
    signal.addEventListener("abort", () => {
      clearTimeout(timer);
      resolve();
    }, { once: true });
  });
}