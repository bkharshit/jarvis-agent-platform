import { Link, useSearchParams } from "react-router";

import {
  useExecutions,
  type ExecutionStatus,
} from "@/api/queries/executions";
import { Badge } from "@/components/ui/Badge";
import { humanInTheLoop } from "@/capabilities/detail";
import { useCapabilities } from "@/capabilities/useCapabilities";
import { SectionGate } from "@/capabilities/SectionGate";

// Executions list — real payload rows only. The agent filter arrives as
// ?agent= from the agents section's Runs tab. The awaiting-input inbox
// (S10) is gated on the backend's executions.detail.human_in_the_loop.

const STATUS_FILTERS: (ExecutionStatus | "all")[] = [
  "all",
  "queued",
  "running",
  "awaiting_input",
  "succeeded",
  "failed",
  "cancelled",
  "timed_out",
];

function StatusBadge({ status }: { status: string }) {
  const variant =
    status === "succeeded"
      ? "success"
      : status === "running"
        ? "warning"
        : status === "failed" || status === "timed_out"
          ? "danger"
          : status === "awaiting_input"
            ? "brand"
            : "neutral";
  return (
    <Badge variant={variant} className="font-mono">
      {status}
    </Badge>
  );
}

function AwaitingInputInbox() {
  const { data } = useExecutions({ status: "awaiting_input" });
  const runs = data?.items;
  if (runs === undefined || runs.length === 0) return null;
  return (
    <div className="mt-6 rounded-xl border border-violet-900/70 bg-violet-950/30 p-4" data-testid="awaiting-input-inbox">
      <h2 className="text-sm font-medium text-violet-300">Awaiting input</h2>
      <ul className="mt-2 space-y-1">
        {runs.map((run) => (
          <li key={run.run_id} className="flex items-center justify-between gap-4 text-sm">
            <Link
              to={`/executions/${run.run_id}`}
              className="font-mono text-xs text-neutral-100 underline-offset-2 hover:underline"
            >
              {run.run_id.slice(0, 12)}…
            </Link>
            <span className="truncate font-mono text-xs text-neutral-400">{run.input}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function ExecutionsListInner() {
  const [searchParams, setSearchParams] = useSearchParams();
  const agentFilter = searchParams.get("agent");
  const statusFilter = searchParams.get("status");
  // S13 (D62): ?trigger= from the triggers section's runs link — the runs a
  // trigger fired, filterable even after the trigger row is deleted (D57).
  const triggerFilter = searchParams.get("trigger");
  const { data: capabilities } = useCapabilities();
  const inboxEnabled = humanInTheLoop(capabilities);
  const { data, isPending, isError, error } = useExecutions({
    agent: agentFilter,
    status: (statusFilter as ExecutionStatus | null) ?? null,
    trigger: triggerFilter,
  });
  const runs = data?.items;
  const names = data?.names ?? {};

  function setFilter(key: "status", value: string) {
    const next = new URLSearchParams(searchParams);
    if (value === "all" || value === "") next.delete(key);
    else next.set(key, value);
    setSearchParams(next, { replace: true });
  }

  return (
    <div className="mx-auto max-w-5xl px-6 py-8">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">
          Executions{agentFilter ? ` — agent ${agentFilter}` : ""}
          {triggerFilter ? " — by trigger" : ""}
        </h1>
        <div className="flex gap-1" role="group" aria-label="Status filter">
          {STATUS_FILTERS.map((s) => (
            <button
              key={s}
              type="button"
              onClick={() => setFilter("status", s)}
              aria-pressed={(statusFilter ?? "all") === s}
              className={`cursor-pointer rounded-md px-2.5 py-1 font-mono text-xs transition-colors ${
                (statusFilter ?? "all") === s
                  ? "bg-indigo-600 text-white"
                  : "text-neutral-400 hover:bg-neutral-800/60 hover:text-neutral-100"
              }`}
            >
              {s}
            </button>
          ))}
        </div>
      </div>

      {isPending && <p className="mt-10 text-sm text-neutral-400">Loading executions…</p>}
      {isError && <p className="mt-10 text-sm text-red-400">{error.message}</p>}

      {inboxEnabled && <AwaitingInputInbox />}

      {runs && (runs.length === 0 ? (
        <p className="mt-10 text-sm text-neutral-400">No executions match.</p>
      ) : (
        <table className="mt-6 w-full text-left text-sm">
          <thead className="text-neutral-400">
            <tr className="border-b border-neutral-800/80">
              <th className="py-2 pr-4 font-medium">Run</th>
              <th className="py-2 pr-4 font-medium">Status</th>
              <th className="py-2 pr-4 font-medium">Agent</th>
              <th className="py-2 pr-4 font-medium">Session</th>
              <th className="py-2 pr-4 font-medium">Started</th>
            </tr>
          </thead>
          <tbody>
            {runs.map((run) => (
              <tr key={run.run_id} className="border-b border-neutral-800/50 transition-colors hover:bg-neutral-900/40">
                <td className="py-2 pr-4">
                  <Link
                    to={`/executions/${run.run_id}`}
                    className="font-mono text-xs text-neutral-100 underline-offset-2 hover:underline"
                  >
                    {run.run_id.slice(0, 12)}…
                  </Link>
                </td>
                <td className="py-2 pr-4"><StatusBadge status={run.status} /></td>
                {/* S6, D41: a workflow run's agent_id resolves through the
                    workflows repo — show the name when the payload carries one. */}
                <td className="py-2 pr-4 font-mono text-xs text-neutral-400" title={run.agent_id}>
                  {(names[run.agent_id] ?? run.agent_id).slice(0, 24)}
                </td>
                <td className="py-2 pr-4 font-mono text-xs text-neutral-400">
                  {run.session_id ?? "—"}
                </td>
                <td className="py-2 pr-4 text-neutral-400">
                  {run.started_at ? new Date(run.started_at).toLocaleString() : "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ))}
    </div>
  );
}

export function ExecutionsList() {
  return (
    <SectionGate sectionKey="executions">
      <ExecutionsListInner />
    </SectionGate>
  );
}