import { Link } from "react-router";

import { useTraces } from "@/api/queries/traces";

// Observability list (S7, ADR 0021 §7, D77): the tenant's traces, newest
// first, from the spans table. Every trace carries the runs that produced
// it (children included — one trace per multi-agent conversation).

function fmtTime(iso: string): string {
  return new Date(iso).toLocaleString();
}

export function ObservabilityPage() {
  const { data: traces, isPending, isError, error } = useTraces();

  if (isPending) {
    return <p className="px-6 py-10 text-sm text-neutral-400">Loading traces…</p>;
  }
  if (isError) {
    return (
      <div className="px-6 py-10">
        <p className="text-sm text-red-400">{error.message}</p>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-4xl px-6 py-8">
      <h1 className="text-xl font-semibold">Observability</h1>
      <p className="mt-1 text-sm text-neutral-400">
        Spans recorded for each run, grouped by trace — the run's trace_id
        covers its sub-agent children too.
      </p>

      {traces.length === 0 ? (
        <p className="py-16 text-center text-sm text-neutral-500">
          No traces yet. Run an agent to produce spans.
        </p>
      ) : (
        <table className="mt-6 w-full text-left text-sm">
          <thead className="text-neutral-400">
            <tr className="border-b border-neutral-800/80">
              <th className="py-2 pr-4 font-medium">Trace</th>
              <th className="py-2 pr-4 font-medium">Runs</th>
              <th className="py-2 pr-4 font-medium">Spans</th>
              <th className="py-2 pr-4 font-medium">Kinds</th>
              <th className="py-2 pr-4 font-medium">Status</th>
              <th className="py-2 pr-4 font-medium">Started</th>
            </tr>
          </thead>
          <tbody>
            {traces.map((trace) => (
              <tr key={trace.trace_id} className="border-b border-neutral-800/50 transition-colors hover:bg-neutral-900/40">
                <td className="py-2 pr-4">
                  <Link
                    to={`/observability/${trace.trace_id}`}
                    className="font-mono text-xs text-neutral-100 underline-offset-2 hover:underline"
                  >
                    {trace.trace_id.slice(0, 8)}…
                  </Link>
                </td>
                <td className="py-2 pr-4 text-neutral-300">{trace.run_ids.length}</td>
                <td className="py-2 pr-4 text-neutral-300">{trace.span_count}</td>
                <td className="py-2 pr-4 font-mono text-xs text-neutral-300">
                  {trace.kinds.join(", ")}
                </td>
                <td className="py-2 pr-4">
                  <span
                    className={
                      trace.statuses.includes("error") ? "text-rose-400" : "text-emerald-400"
                    }
                  >
                    {trace.statuses.join(", ")}
                  </span>
                </td>
                <td className="py-2 pr-4 text-neutral-400">{fmtTime(trace.started_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}