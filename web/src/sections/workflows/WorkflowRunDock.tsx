import { useEffect, useRef, useState } from "react";

import { client } from "@/api/client";
import { connectWorkflowRunStream, type RunStreamHandle, type StreamStatus } from "@/api/sse";
import { EventTimeline } from "@/components/EventTimeline";
import { PauseCard } from "@/components/PauseCard";
import { Button } from "@/components/ui/Button";
import { Textarea } from "@/components/ui/Textarea";
import { useRunConsoleStore, type WireEvent } from "@/stores/runConsole";

// Workflow run dock (S6 → UI modernization): the run console's live-SSE
// body, extracted from the old standalone page so the editor hosts it in
// the Test-run slide-over. The wiring is the agent RunDock's shape against
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

export function WorkflowRunDock({ workflowId }: { workflowId: string }) {
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
    <div className="flex flex-col gap-4">
      <div className="flex flex-col gap-3">
        <Textarea
          aria-label="Run input"
          rows={3}
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="Workflow input (each agent node may template it with {{input}})"
        />
        <div className="flex flex-wrap items-center gap-3">
          <Button
            onClick={startRun}
            disabled={runActive || input.trim() === ""}
          >
            {runActive ? "Running…" : "Run"}
          </Button>
          {canResume && (
            <Button
              variant="secondary"
              onClick={connect}
              className="border-amber-700 text-amber-200 hover:bg-amber-950"
            >
              Resume
            </Button>
          )}
          {runId !== null && (status === "running" || status === "awaiting_input") && (
            <Button
              variant="secondary"
              onClick={() => {
                void client.POST("/v1/executions/{run_id}/cancel", {
                  params: { path: { run_id: runId } },
                });
              }}
              className="border-red-800 text-red-300 hover:bg-red-950"
            >
              Cancel
            </Button>
          )}
          {streamStatusLabel && (
            <span className="text-xs text-neutral-400" aria-live="polite">
              {streamStatusLabel}
            </span>
          )}
        </div>
      </div>

      <EventTimeline items={items} />

      {status === "awaiting_input" && pause && (
        <PauseCard
          question={pause.question}
          pendingCalls={pause.pendingCalls}
          busy={answering}
          onSubmit={(body) => void sendResume(body)}
        />
      )}

      {status === "completed" && finalMessage !== null && (
        <div
          className="rounded-xl border border-green-900 bg-green-950/40 p-4"
          data-testid="final-message"
        >
          <p className="text-xs text-green-400">Final answer</p>
          <p className="mt-1 whitespace-pre-wrap text-sm text-neutral-100">{finalMessage}</p>
        </div>
      )}
      {status === "failed" && runError !== null && (
        <div
          className="rounded-xl border border-red-900 bg-red-950/40 p-4"
          data-testid="run-error"
        >
          <p className="text-xs text-red-400">
            Run failed{runError.kind ? ` (${runError.kind})` : ""}
          </p>
          <p className="mt-1 text-sm text-neutral-100">{runError.message}</p>
        </div>
      )}
      {status === "cancelled" && runError !== null && (
        <div
          className="rounded-xl border border-amber-900 bg-amber-950/40 p-4"
          data-testid="run-cancelled"
        >
          <p className="text-xs text-amber-400">Run cancelled</p>
          <p className="mt-1 text-sm text-neutral-100">{runError.message}</p>
        </div>
      )}
    </div>
  );
}