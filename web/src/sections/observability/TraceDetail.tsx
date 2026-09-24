import { Link, useParams } from "react-router";

import { useTrace } from "@/api/queries/traces";
import { Badge } from "@/components/ui/Badge";

import type { SpanRecord } from "@/api/queries/traces";

// Trace detail (S7, D77): one trace's spans as a waterfall — rows in
// chronological order, children indented under their parent, a duration
// bar sized against the trace's own span, and honest status coloring.

const KIND_LABELS: Record<string, string> = {
  "run.segment": "segment",
  iteration: "iteration",
  "llm.call": "llm",
  "tool.call": "tool",
  "knowledge.retrieve": "retrieve",
};

function fmtDuration(ms: number): string {
  if (ms >= 1000) return `${(ms / 1000).toFixed(2)}s`;
  return `${Math.round(ms)}ms`;
}

/** Depth per span (parent linkage rides parent_span_id), computed once. */
function depths(spans: SpanRecord[]): Map<string, number> {
  // The generated schema types the id fields optional (backend defaults);
  // an id-less span is simply a root.
  const byId = new Map(
    spans.filter((span) => span.span_id !== undefined).map((span) => [span.span_id as string, span]),
  );
  const memo = new Map<string, number>();
  const depthOf = (span: SpanRecord): number => {
    if (span.span_id === undefined) return 0;
    const cached = memo.get(span.span_id);
    if (cached !== undefined) return cached;
    if (!span.parent_span_id || !byId.has(span.parent_span_id)) return 0;
    const parent = byId.get(span.parent_span_id);
    const depth = parent ? depthOf(parent) + 1 : 0;
    memo.set(span.span_id, depth);
    return depth;
  };
  for (const span of spans) depthOf(span);
  return memo;
}

function SpanRow({
  span,
  depth,
  offsetMs,
  traceMs,
}: {
  span: SpanRecord;
  depth: number;
  offsetMs: number;
  traceMs: number;
}) {
  const statusClass =
    span.status === "error"
      ? "text-red-400"
      : span.status === "cancelled"
        ? "text-amber-400"
        : "text-emerald-400";
  const kindLabel = KIND_LABELS[span.kind] ?? span.kind;
  return (
    <tr className="border-b border-neutral-800/50 transition-colors hover:bg-neutral-900/40">
      <td className="py-2 pr-4">
        <div style={{ paddingLeft: `${depth * 16}px` }}>
          <span className="font-mono text-xs text-neutral-100">{span.name}</span>
          <Badge variant="neutral" className="ml-2 font-mono">
            {kindLabel}
          </Badge>
        </div>
      </td>
      <td className="py-2 pr-4">
        <span className={statusClass}>{span.status}</span>
        {span.status_message ? (
          <span className="ml-2 text-xs text-neutral-500" title={span.status_message}>
            {span.status_message.length > 60
              ? `${span.status_message.slice(0, 60)}…`
              : span.status_message}
          </span>
        ) : null}
      </td>
      <td className="py-2 pr-4 text-neutral-400">
        {traceMs > 0 ? `${Math.round(offsetMs)}ms` : "0ms"}
      </td>
      <td className="py-2 pr-4 text-neutral-300">{fmtDuration(span.duration_ms)}</td>
      <td className="py-2" style={{ minWidth: "120px" }}>
        {traceMs > 0 && (
          <div
            className="h-2 rounded-full bg-indigo-500/70"
            style={{
              marginLeft: `${Math.min(100, (offsetMs / traceMs) * 100)}%`,
              width: `${Math.max(1, Math.min(100 - (offsetMs / traceMs) * 100, (span.duration_ms / traceMs) * 100))}%`,
            }}
          />
        )}
      </td>
    </tr>
  );
}

export function TraceDetailPage() {
  const { traceId = "" } = useParams();
  const { data: detail, isPending, isError, error } = useTrace(traceId);

  if (isPending) {
    return <p className="px-6 py-10 text-sm text-neutral-400">Loading trace…</p>;
  }
  if (isError) {
    return (
      <div className="mx-auto max-w-4xl px-6 py-8">
        <p className="text-sm text-red-400">{error.message}</p>
        <Link to="/observability" className="mt-4 inline-block text-sm text-neutral-300 underline">
          Back to traces
        </Link>
      </div>
    );
  }

  const spans = detail.spans;
  const startedAt = spans.length
    ? new Date(spans[0].started_at ?? 0).getTime()
    : 0;
  const lastEnd = spans.reduce(
    (max, span) => Math.max(max, new Date(span.ended_at ?? 0).getTime()),
    startedAt,
  );
  const traceMs = Math.max(0, lastEnd - startedAt);

  const depthsMap = depths(spans);
  return (
    <div className="mx-auto max-w-4xl px-6 py-8">
      <div className="flex items-center gap-3">
        <Link to="/observability" className="text-sm text-neutral-400 hover:text-neutral-200">
          ← Traces
        </Link>
        <h1 className="font-mono text-lg font-semibold text-neutral-100">{detail.trace_id}</h1>
      </div>
      <p className="mt-1 text-sm text-neutral-400">
        {spans.length} spans across runs {spans.map((s) => s.run_id).filter((v, i, a) => a.indexOf(v) === i).join(", ")}
      </p>

      <table className="mt-6 w-full text-left text-sm">
        <thead className="text-neutral-400">
          <tr className="border-b border-neutral-800/80">
            <th className="py-2 pr-4 font-medium">Span</th>
            <th className="py-2 pr-4 font-medium">Status</th>
            <th className="py-2 pr-4 font-medium">Offset</th>
            <th className="py-2 pr-4 font-medium">Duration</th>
            <th className="py-2 font-medium">Timeline</th>
          </tr>
        </thead>
        <tbody>
          {spans.map((span) => (
            <SpanRow
              key={span.span_id}
              span={span}
              depth={span.span_id !== undefined ? depthsMap.get(span.span_id) ?? 0 : 0}
              offsetMs={startedAt ? new Date(span.started_at ?? 0).getTime() - startedAt : 0}
              traceMs={traceMs}
            />
          ))}
        </tbody>
      </table>
    </div>
  );
}