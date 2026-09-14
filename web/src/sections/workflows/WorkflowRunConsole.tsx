import { useEffect, useRef, useState } from "react";
import { useParams } from "react-router";

import { client } from "@/api/client";
import { connectWorkflowRunStream, type RunStreamHandle, type StreamStatus } from "@/api/sse";
import { EventTimeline } from "@/components/EventTimeline";
import { PauseCard } from "@/components/PauseCard";
import { SectionGate } from "@/capabilities/SectionGate";
import { flushPending, useRunConsoleStore, type WireEvent } from "@/stores/runConsole";

// Workflow run console (S6): the agent RunConsole's live-SSE shape against
// the workflow stream route (D41 — only the path differs). Node events
// (D43) group into per-node sections through the shared reducer; a pause
// INSIDE a node ends the segment like any pause and the same PauseCard
// collects the answer (ADR 0015 §7).

const STATUS_LABEL: Record<string, string> = {
  connecting: "Connecting…",
  live: "Live",
  reconnecting: "Reconnecting…",
  disconnected: "Disconnected",
  finished: "Finished",
  error: "Error",
};

function ConsoleInner() {
  const { workflowId } = useParams();
  const [input, setInput] = useState("");
  const [streamStatus, setStreamStatus] = useState<StreamStatus | null>(null);
  const [starting, setStarting] = useState(false);
  const [answering, setAnswering] = useState(false);

  const apply = useRunConsoleStore((s) => s.apply);
  const settle = useRunConsoleStore((s) => s.settle);
  const reset = useRunConsoleStore((s) => s.reset);
  const status = useRunConsoleStore((s) => s.status);
  const items = useRunConsoleStore((s) => s.items);
  const finalMessage = useRunConsoleStore((s) => s.finalMessage);
  const runError = useRunConsoleStore((s) => s.error);
  const runId = useRunConsoleStore((s) => s.runId);
  const pause = useRunConsoleStore((s) => s.pause);

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
    if (event.type === "run.started") {
      runIdRef.current = event.run_id;
      setStarting(false);
    }
    apply(event, frame.id);
  }

  function connect() {
    if (workflowId === undefined) return;
    handleRef.current?.abort();
    handleRef.current = connectWorkflowRunStream(workflowId, {
      body: { input: input },
      getLastEventId: () => lastEventIdRef.current,
      getRunId: () => runIdRef.current,
      onFrame,
      onStatus: (next) => {
        setStreamStatus(next);
        if (next.status === "finished" || next.status === "disconnected" || next.status === "error") {
          settle();
        }
        if (next.status !== "connecting" && next.status !== "live") setStarting(false);
      },
    });
  }

  function startRun() {
    if (input.trim() === "") return;
    reset();
    lastEventIdRef.current = null;
    runIdRef.current = null;
    setStarting(true);
    connect();
  }

  async function sendResume(body: {
    content?: string;
    tool_approval?: boolean;
    decisions?: Record<string, boolean>;
  }) {
    if (runId === null) return;
    setAnswering(true);
    try {
      await client.POST("/v1/executions/{run_id}/resume", {
        params: { path: { run_id: runId } },
        body,
      });
      connect();
    } finally {
      setAnswering(false);
    }
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
      <h1 className="text-xl font-semibold">Run workflow</h1>

      <div className="mt-4 flex flex-col gap-3">
        <textarea
          aria-label="Run input"
          rows={3}
          className="w-full rounded border border-neutral-700 bg-neutral-900 px-2 py-1.5 text-sm text-neutral-100 focus:border-neutral-500 focus:outline-none"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="Workflow input (each agent node may template it with {{input}})"
        />
        <div className="flex items-center gap-3">
          <button
            type="button"
            onClick={startRun}
            disabled={runActive || input.trim() === ""}
            className="cursor-pointer rounded bg-neutral-100 px-4 py-1.5 text-sm font-medium text-neutral-900 hover:bg-white disabled:cursor-not-allowed disabled:text-neutral-500"
          >
            {runActive ? "Running…" : "Run"}
          </button>
          {canResume && (
            <button
              type="button"
              onClick={connect}
              className="cursor-pointer rounded border border-amber-700 px-4 py-1.5 text-sm text-amber-200 hover:bg-amber-950"
            >
              Resume
            </button>
          )}
          {runId !== null && (status === "running" || status === "awaiting_input") && (
            <button
              type="button"
              onClick={() => {
                void client.POST("/v1/executions/{run_id}/cancel", {
                  params: { path: { run_id: runId } },
                });
              }}
              className="cursor-pointer rounded border border-red-800 px-4 py-1.5 text-sm text-red-300 hover:bg-red-950"
            >
              Cancel
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
        <div className="mt-4">
          <PauseCard
            question={pause.question}
            pendingCalls={pause.pendingCalls}
            busy={answering}
            onSubmit={(body) => void sendResume(body)}
          />
        </div>
      )}

      {status === "completed" && finalMessage !== null && (
        <div className="mt-4 rounded border border-green-900 bg-green-950/40 p-4" data-testid="final-message">
          <p className="text-xs text-green-400">Final answer</p>
          <p className="mt-1 whitespace-pre-wrap text-sm text-neutral-100">{finalMessage}</p>
        </div>
      )}
      {status === "failed" && runError !== null && (
        <div className="mt-4 rounded border border-red-900 bg-red-950/40 p-4" data-testid="run-error">
          <p className="text-xs text-red-400">
            Run failed{runError.kind ? ` (${runError.kind})` : ""}
          </p>
          <p className="mt-1 text-sm text-neutral-100">{runError.message}</p>
        </div>
      )}
      {status === "cancelled" && runError !== null && (
        <div className="mt-4 rounded border border-amber-900 bg-amber-950/40 p-4" data-testid="run-cancelled">
          <p className="text-xs text-amber-400">Run cancelled</p>
          <p className="mt-1 text-sm text-neutral-100">{runError.message}</p>
        </div>
      )}
    </div>
  );
}

export function WorkflowRunConsole() {
  return (
    <SectionGate sectionKey="workflows">
      <ConsoleInner />
    </SectionGate>
  );
}

// Re-export: replay folds through the exact same pure reducer.
export { flushPending };