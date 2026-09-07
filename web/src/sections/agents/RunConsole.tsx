import { useEffect, useRef, useState } from "react";
import { useParams } from "react-router";

import { client } from "@/api/client";
import {
  connectRunStream,
  type RunStreamHandle,
  type StreamStatus,
} from "@/api/sse";
import { EventTimeline } from "@/components/EventTimeline";
import { SectionGate } from "@/capabilities/SectionGate";
import {
  flushPending,
  useRunConsoleStore,
  type WireEvent,
} from "@/stores/runConsole";

// Run console: live SSE with cancel and Last-Event-ID resume (decision 3).
// A stream end is never treated as completion — only a terminal event (or
// an explicit Resume after `disconnected`) resolves the run. A pause (S10)
// also ends the stream: the pause card collects the human's answer, POSTs
// /executions/{id}/resume (blocking), then re-attaches the stream at the
// last cursor so the resumed segment replays into the same timeline.

const STATUS_LABEL: Record<string, string> = {
  connecting: "Connecting…",
  live: "Live",
  reconnecting: "Reconnecting…",
  disconnected: "Disconnected",
  finished: "Finished",
  error: "Error",
};

function RunConsoleInner() {
  const { agentId } = useParams();
  const [input, setInput] = useState("");
  const [sessionId, setSessionId] = useState("");
  const [streamStatus, setStreamStatus] = useState<StreamStatus | null>(null);
  const [starting, setStarting] = useState(false);
  /** A resume POST is in flight (it blocks until the segment ends). */
  const [answering, setAnswering] = useState(false);
  /** The pending answer text for a question pause. */
  const [answer, setAnswer] = useState("");
  /** Staged per-call verdicts for an approval pause (ADR 0011) — the
   * Submit posts the map; a call left undecided keeps Submit disabled. */
  const [decisions, setDecisions] = useState<Record<string, boolean>>({});

  const apply = useRunConsoleStore((s) => s.apply);
  const settle = useRunConsoleStore((s) => s.settle);
  const reset = useRunConsoleStore((s) => s.reset);
  const status = useRunConsoleStore((s) => s.status);
  const items = useRunConsoleStore((s) => s.items);
  const finalMessage = useRunConsoleStore((s) => s.finalMessage);
  const runError = useRunConsoleStore((s) => s.error);
  const usage = useRunConsoleStore((s) => s.usage);
  const runId = useRunConsoleStore((s) => s.runId);
  const pause = useRunConsoleStore((s) => s.pause);

  // Refs the connector reads at (re)connect time — no stale closures.
  const handleRef = useRef<RunStreamHandle | null>(null);
  const lastEventIdRef = useRef<number | null>(null);
  const runIdRef = useRef<string | null>(null);

  useEffect(() => {
    return () => {
      handleRef.current?.abort();
      settle();
    };
  }, [settle]);

  function onFrame(frame: { id: number | null; event: string; data: string }) {
    if (frame.id !== null) lastEventIdRef.current = frame.id;
    const event = JSON.parse(frame.data) as WireEvent;
    if (event.type === "run.started") runIdRef.current = event.run_id;
    apply(event, frame.id);
  }

  function connect() {
    if (agentId === undefined) return;
    handleRef.current?.abort();
    handleRef.current = connectRunStream(agentId, {
      body: { input: input, session_id: sessionId || null },
      getLastEventId: () => lastEventIdRef.current,
      getRunId: () => runIdRef.current,
      onFrame,
      onStatus: (next) => {
        setStreamStatus(next);
        if (next.status === "finished") settle();
        if (next.status === "disconnected") settle();
        if (next.status === "error") settle();
      },
    });
  }

  function startRun() {
    if (input.trim() === "") return;
    reset();
    setDecisions({});
    lastEventIdRef.current = null;
    runIdRef.current = null;
    setStarting(true);
    connect();
  }

  function resume() {
    connect();
  }

  async function sendResume(body: {
    content?: string;
    tool_approval?: boolean;
    decisions?: Record<string, boolean>;
  }) {
    if (runId === null) return;
    setAnswering(true);
    setAnswer("");
    setDecisions({});
    try {
      // Blocking: the route returns once the resumed segment ends (it may
      // pause again). The stream is re-attached afterwards at the pause
      // cursor so the segment's events replay into the timeline (deduped).
      await client.POST("/v1/executions/{run_id}/resume", {
        params: { path: { run_id: runId } },
        body,
      });
      connect();
    } finally {
      setAnswering(false);
    }
  }

  function cancelRun() {
    if (runId === null) return;
    void client.POST("/v1/executions/{run_id}/cancel", {
      params: { path: { run_id: runId } },
    });
  }

  const streamStatusLabel =
    streamStatus === null
      ? null
      : streamStatus.status === "reconnecting"
        ? `${STATUS_LABEL.reconnecting} (attempt ${streamStatus.attempt}/3)`
        : streamStatus.status === "error"
          ? `${STATUS_LABEL.error}: ${streamStatus.message}`
          : STATUS_LABEL[streamStatus.status];
  const runActive = status === "running" || status === "awaiting_input" || starting;
  const canResume = streamStatus?.status === "disconnected";

  return (
    <div className="mx-auto max-w-4xl px-6 py-8">
      <h1 className="text-xl font-semibold">Run agent</h1>

      <div className="mt-4 flex flex-col gap-3">
        <textarea
          aria-label="Run input"
          rows={3}
          className="w-full rounded border border-neutral-700 bg-neutral-900 px-2 py-1.5 text-sm text-neutral-100 focus:border-neutral-500 focus:outline-none"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="What should the agent do?"
        />
        <div className="flex items-center gap-3">
          <input
            aria-label="Session id (optional)"
            className="w-64 rounded border border-neutral-700 bg-neutral-900 px-2 py-1.5 text-sm text-neutral-100"
            value={sessionId}
            onChange={(e) => setSessionId(e.target.value)}
            placeholder="session id (optional)"
          />
          <button
            type="button"
            onClick={startRun}
            disabled={runActive || input.trim() === ""}
            className="cursor-pointer rounded bg-neutral-100 px-4 py-1.5 text-sm font-medium text-neutral-900 hover:bg-white disabled:cursor-not-allowed disabled:text-neutral-500"
          >
            {runActive ? "Running…" : "Run"}
          </button>
          {runId !== null && (status === "running" || status === "awaiting_input") && (
            <button
              type="button"
              onClick={cancelRun}
              className="cursor-pointer rounded border border-red-800 px-4 py-1.5 text-sm text-red-300 hover:bg-red-950"
            >
              Cancel
            </button>
          )}
          {canResume && (
            <button
              type="button"
              onClick={resume}
              className="cursor-pointer rounded border border-amber-700 px-4 py-1.5 text-sm text-amber-200 hover:bg-amber-950"
            >
              Resume
            </button>
          )}
          {streamStatusLabel && (
            <span className="text-xs text-neutral-400" aria-live="polite">
              {streamStatusLabel}
            </span>
          )}
        </div>
      </div>

      <div className="mt-6">
        <EventTimeline items={items} />
      </div>

      {status === "awaiting_input" && pause && (
        <div className="mt-4 rounded border border-violet-800 bg-violet-950/40 p-4" data-testid="pause-card">
          <p className="text-xs text-violet-300">Waiting for your input{answering ? " — resuming…" : ""}</p>
          {pause.question !== null && (
            <p className="mt-1 whitespace-pre-wrap text-sm text-neutral-100">{pause.question}</p>
          )}
          {pause.pendingCalls.length > 0 && (
            <>
              <ul className="mt-2 space-y-2">
                {pause.pendingCalls.map((call) => {
                  const chosen = decisions[call.id];
                  return (
                    <li
                      key={call.id}
                      className="flex items-center justify-between gap-3 rounded border border-neutral-800 bg-neutral-900 px-3 py-2"
                      data-testid={`pending-call-${call.id}`}
                    >
                      <span className="min-w-0 text-sm text-neutral-200">
                        <span className="font-medium">{call.name}</span>{" "}
                        <span className="break-all font-mono text-xs text-neutral-400">
                          {JSON.stringify(call.arguments)}
                        </span>
                      </span>
                      <span className="flex shrink-0 gap-2">
                        <button
                          type="button"
                          aria-pressed={chosen === true}
                          disabled={answering}
                          onClick={() =>
                            setDecisions((d) => ({ ...d, [call.id]: true }))
                          }
                          className={
                            chosen === true
                              ? "cursor-pointer rounded border border-green-500 bg-green-900 px-3 py-1 text-xs font-medium text-green-200 disabled:cursor-not-allowed disabled:text-neutral-600"
                              : "cursor-pointer rounded border border-green-800 px-3 py-1 text-xs text-green-300 hover:bg-green-950 disabled:cursor-not-allowed disabled:text-neutral-600"
                          }
                        >
                          Approve
                        </button>
                        <button
                          type="button"
                          aria-pressed={chosen === false}
                          disabled={answering}
                          onClick={() =>
                            setDecisions((d) => ({ ...d, [call.id]: false }))
                          }
                          className={
                            chosen === false
                              ? "cursor-pointer rounded border border-red-500 bg-red-900 px-3 py-1 text-xs font-medium text-red-200 disabled:cursor-not-allowed disabled:text-neutral-600"
                              : "cursor-pointer rounded border border-red-800 px-3 py-1 text-xs text-red-300 hover:bg-red-950 disabled:cursor-not-allowed disabled:text-neutral-600"
                          }
                        >
                          Reject
                        </button>
                      </span>
                    </li>
                  );
                })}
              </ul>
              <div className="mt-3 flex items-center justify-between gap-3">
                <button
                  type="button"
                  data-testid="allow-all"
                  disabled={answering}
                  onClick={() =>
                    setDecisions(
                      Object.fromEntries(pause.pendingCalls.map((c) => [c.id, true])),
                    )
                  }
                  className="cursor-pointer rounded border border-neutral-700 px-3 py-1 text-xs text-neutral-300 hover:bg-neutral-900 disabled:cursor-not-allowed disabled:text-neutral-600"
                >
                  Allow all
                </button>
                <button
                  type="button"
                  data-testid="submit-decisions"
                  disabled={
                    answering ||
                    pause.pendingCalls.some((c) => decisions[c.id] === undefined)
                  }
                  onClick={() => void sendResume({ decisions })}
                  className="cursor-pointer rounded bg-neutral-100 px-4 py-1.5 text-sm font-medium text-neutral-900 hover:bg-white disabled:cursor-not-allowed disabled:text-neutral-500"
                >
                  Submit decision
                </button>
              </div>
            </>
          )}
          {pause.pendingCalls.length === 0 && pause.question !== null && (
            <form
              className="mt-3 flex gap-2"
              onSubmit={(e) => {
                e.preventDefault();
                if (answer.trim() === "") return;
                void sendResume({ content: answer });
              }}
            >
              <input
                aria-label="Your answer"
                className="min-w-0 flex-1 rounded border border-neutral-700 bg-neutral-900 px-2 py-1.5 text-sm text-neutral-100 focus:border-neutral-500 focus:outline-none"
                value={answer}
                disabled={answering}
                onChange={(e) => setAnswer(e.target.value)}
                placeholder="Your answer…"
              />
              <button
                type="submit"
                disabled={answering || answer.trim() === ""}
                className="cursor-pointer rounded bg-neutral-100 px-4 py-1.5 text-sm font-medium text-neutral-900 hover:bg-white disabled:cursor-not-allowed disabled:text-neutral-500"
              >
                Send answer
              </button>
            </form>
          )}
        </div>
      )}

      {status === "completed" && finalMessage !== null && (
        <div className="mt-4 rounded border border-green-900 bg-green-950/40 p-4" data-testid="final-message">
          <p className="text-xs text-green-400">Final answer</p>
          <p className="mt-1 whitespace-pre-wrap text-sm text-neutral-100">
            {finalMessage}
          </p>
        </div>
      )}
      {status === "failed" && runError !== null && (
        <div className="mt-4 rounded border border-red-900 bg-red-950/40 p-4" data-testid="run-error">
          <p className="text-xs text-red-400">Run failed{runError.kind ? ` (${runError.kind})` : ""}</p>
          <p className="mt-1 text-sm text-neutral-100">{runError.message}</p>
        </div>
      )}
      {status === "cancelled" && runError !== null && (
        <div className="mt-4 rounded border border-amber-900 bg-amber-950/40 p-4" data-testid="run-cancelled">
          <p className="text-xs text-amber-400">Run cancelled</p>
          <p className="mt-1 text-sm text-neutral-100">{runError.message}</p>
        </div>
      )}
      {usage && (
        <p className="mt-3 text-xs text-neutral-500">
          tokens: {usage.input_tokens} in / {usage.output_tokens} out
        </p>
      )}
    </div>
  );
}

export function RunConsole() {
  return (
    <SectionGate sectionKey="agents">
      <RunConsoleInner />
    </SectionGate>
  );
}

// Kept exported for replay (commit 8) to reuse the exact fold.
export { flushPending };