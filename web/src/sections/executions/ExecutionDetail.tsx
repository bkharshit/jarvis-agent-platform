import { useEffect, useState } from "react";
import { Link, useParams } from "react-router";
import { useQueryClient } from "@tanstack/react-query";

import { client, unwrap } from "@/api/client";
import {
  executionQueryKey,
  useExecution,
  useReplayEvents,
} from "@/api/queries/executions";
import { contentToText } from "@/components/messageContent";
import { EventTimeline } from "@/components/EventTimeline";
import { PauseCard } from "@/components/PauseCard";
import { SectionGate } from "@/capabilities/SectionGate";
import {
  flushPending,
  initialRunConsoleState,
  applyEvent,
  type PendingCallView,
  type TimelineItem,
  type WireEvent,
} from "@/stores/runConsole";

// Execution detail: run summary, transcript, tool executions — and replay,
// which folds the JSON event log through the SAME applyEvent reducer the
// live console uses, so both views render identically (decision 4). S10:
// an awaiting_input run renders the shared PauseCard here too — a pause is
// durable, so the human can answer it from this page, not only from the
// live console it happened in.

function SummaryRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex gap-2 text-sm">
      <span className="w-32 shrink-0 text-neutral-400">{label}</span>
      <span className="min-w-0 break-words text-neutral-200">{value}</span>
    </div>
  );
}

/** Pure replay projection: the event log → the same timeline the live console renders. */
export function projectReplay(events: WireEvent[]): TimelineItem[] {
  let state = initialRunConsoleState();
  for (const event of events) {
    state = applyEvent(state, event, null);
  }
  return flushPending(state).items;
}

function ReplayView({ runId }: { runId: string }) {
  const { data, isPending, isError, error } = useReplayEvents(runId);
  const [items, setItems] = useState<TimelineItem[]>([]);

  useEffect(() => {
    if (data) setItems(projectReplay(data.events.map((pair) => pair.event)));
  }, [data]);

  if (isPending) {
    return <p className="text-sm text-neutral-400">Loading replay…</p>;
  }
  if (isError) {
    return <p className="text-sm text-red-400">{error.message}</p>;
  }
  return (
    <div className="mt-4" data-testid="replay-timeline">
      <EventTimeline items={items} />
    </div>
  );
}

/** The LAST run.awaiting_input in the log is the live pause — earlier ones
 * were answered (any later event proves the run moved on). */
function lastPauseFromEvents(
  events: { event: WireEvent }[] | undefined,
): { question: string | null; pendingCalls: PendingCallView[] } | null {
  if (events === undefined) return null;
  for (let i = events.length - 1; i >= 0; i--) {
    const e = events[i].event;
    if (e.type === "run.awaiting_input") {
      return {
        question: e.question === "" ? null : e.question,
        pendingCalls: (e.pending_calls ?? []).map((call) => ({
          id: call.id,
          name: call.name,
          arguments: call.arguments ?? {},
        })),
      };
    }
  }
  return null;
}

function ExecutionDetailInner() {
  const { runId } = useParams();
  const [showReplay, setShowReplay] = useState(false);
  const { data: detail, isPending, isError, error } = useExecution(runId);
  const queryClient = useQueryClient();
  /** A resume/cancel POST is in flight. */
  const [acting, setActing] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const awaiting = detail?.run.status === "awaiting_input";
  const { data: events } = useReplayEvents(awaiting ? runId : undefined);
  const pause = awaiting ? lastPauseFromEvents(events?.events) : null;

  if (runId === undefined) {
    return (
      <p className="px-6 py-10 text-sm text-neutral-400">
        <Link to="/executions" className="underline">Back to executions</Link>
      </p>
    );
  }
  if (isPending) {
    return <p className="px-6 py-10 text-sm text-neutral-400">Loading execution…</p>;
  }
  if (isError) {
    return (
      <div className="px-6 py-10">
        <p className="text-sm text-red-400">{error.message}</p>
        <Link to="/executions" className="mt-4 inline-block text-sm text-neutral-300 underline">
          Back to executions
        </Link>
      </div>
    );
  }

  const { run, messages, tool_executions } = detail;

  async function resume(body: { content?: string; decisions?: Record<string, boolean> }) {
    setActing(true);
    setActionError(null);
    try {
      // Blocking: returns once the resumed segment ends — it may pause
      // again, in which case the refetched row + events render the next
      // pause card.
      await unwrap(
        client.POST("/v1/executions/{run_id}/resume", {
          params: { path: { run_id: runId! } },
          body,
        }),
      );
      // Prefix key: refreshes both the detail row and the events replay.
      await queryClient.invalidateQueries({ queryKey: executionQueryKey(runId!) });
    } catch (e) {
      setActionError(e instanceof Error ? e.message : String(e));
    } finally {
      setActing(false);
    }
  }

  async function cancel() {
    setActing(true);
    setActionError(null);
    try {
      await unwrap(
        client.POST("/v1/executions/{run_id}/cancel", {
          params: { path: { run_id: runId! } },
        }),
      );
      await queryClient.invalidateQueries({ queryKey: executionQueryKey(runId!) });
    } catch (e) {
      setActionError(e instanceof Error ? e.message : String(e));
    } finally {
      setActing(false);
    }
  }

  return (
    <div className="mx-auto max-w-4xl px-6 py-8">
      <div className="flex items-center justify-between">
        <h1 className="font-mono text-lg font-semibold">{run.run_id}</h1>
        <button
          type="button"
          onClick={() => setShowReplay((v) => !v)}
          className="cursor-pointer rounded border border-neutral-700 px-3 py-1.5 text-sm text-neutral-200 hover:bg-neutral-900"
          aria-pressed={showReplay}
        >
          {showReplay ? "Hide replay" : "Replay events"}
        </button>
      </div>

      <div className="mt-4 flex flex-col gap-1.5">
        <SummaryRow label="Status" value={run.status} />
        <SummaryRow label="Agent" value={run.agent_id} />
        <SummaryRow label="Session" value={run.session_id ?? "—"} />
        <SummaryRow label="Input" value={run.input} />
        {run.final_message !== null && run.final_message !== undefined && (
          <SummaryRow label="Final message" value={run.final_message} />
        )}
        {(run.error !== null && run.error !== undefined) && (
          <SummaryRow
            label={`Error (${run.error_kind ?? "unknown"})`}
            value={run.error}
          />
        )}
        <SummaryRow
          label="Usage"
          value={`${run.total_usage?.input_tokens ?? 0} in / ${run.total_usage?.output_tokens ?? 0} out · ${run.iterations ?? 0} iterations`}
        />
        <SummaryRow
          label="Started"
          value={run.started_at ? new Date(run.started_at).toLocaleString() : "—"}
        />
      </div>

      {awaiting && (
        <div className="mt-4" data-testid="awaiting-actions">
          {pause !== null ? (
            <PauseCard
              question={pause.question}
              pendingCalls={pause.pendingCalls}
              busy={acting}
              onSubmit={(body) => void resume(body)}
            />
          ) : (
            <p className="text-sm text-neutral-400">Loading pause…</p>
          )}
          <div className="mt-2 flex items-center gap-3">
            <button
              type="button"
              data-testid="cancel-run"
              disabled={acting}
              onClick={() => void cancel()}
              className="cursor-pointer rounded border border-red-800 px-3 py-1.5 text-sm text-red-300 hover:bg-red-950 disabled:cursor-not-allowed disabled:text-neutral-600"
            >
              Cancel run
            </button>
            {actionError !== null && (
              <p className="text-sm text-red-400" role="alert">{actionError}</p>
            )}
          </div>
        </div>
      )}

      {showReplay && <ReplayView runId={runId} />}

      <section className="mt-8">
        <h2 className="text-sm font-medium text-neutral-300">Transcript</h2>
        {messages.length === 0 ? (
          <p className="mt-2 text-sm text-neutral-500">No messages.</p>
        ) : (
          <ul className="mt-2 flex flex-col gap-2">
            {messages.map((m, i) => (
              <li key={i} className="rounded bg-neutral-900 p-3 text-sm">
                <span className="text-xs text-neutral-400">{m.role}</span>
                <p className="mt-1 whitespace-pre-wrap text-neutral-200">
                  {contentToText(m.content)}
                </p>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="mt-8">
        <h2 className="text-sm font-medium text-neutral-300">Tool executions</h2>
        {tool_executions.length === 0 ? (
          <p className="mt-2 text-sm text-neutral-500">None.</p>
        ) : (
          <ul className="mt-2 flex flex-col gap-2">
            {tool_executions.map((t) => (
              <li
                key={t.tool_call_id}
                className={`rounded border p-3 text-sm ${
                  t.is_error ? "border-red-900 bg-red-950/40" : "border-neutral-800 bg-neutral-900"
                }`}
              >
                <div className="flex items-center justify-between">
                  <span className="font-mono text-xs text-neutral-100">{t.tool_name}</span>
                  <span className="text-xs text-neutral-400">{t.latency_ms} ms</span>
                </div>
                {/* output is top-level on the API payload (D17) */}
                <pre
                  className={`mt-2 max-h-60 overflow-auto whitespace-pre-wrap rounded p-2 font-mono text-xs ${
                    t.is_error ? "text-red-200" : "text-neutral-300"
                  }`}
                >
                  {t.output}
                </pre>
              </li>
            ))}
          </ul>
        )}
      </section>

      <Link to="/executions" className="mt-8 inline-block text-sm text-neutral-400 underline">
        ← All executions
      </Link>
    </div>
  );
}

export function ExecutionDetailPage() {
  return (
    <SectionGate sectionKey="executions">
      <ExecutionDetailInner />
    </SectionGate>
  );
}