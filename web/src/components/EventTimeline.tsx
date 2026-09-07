import type { TimelineItem } from "@/stores/runConsole";

// Shared run-timeline renderer: live SSE and JSON replay produce the same
// item list, so both views render through this one component.

function ToolCard({ item }: { item: Extract<TimelineItem, { kind: "tool" }> }) {
  const statusBadge = {
    requested: "text-neutral-400",
    running: "text-amber-300",
    completed: "text-green-300",
    failed: "text-red-300",
    declined: "text-violet-300",
  }[item.status];

  return (
    <div className="rounded border border-neutral-800 bg-neutral-900/60 p-3">
      <div className="flex items-center justify-between">
        <span className="font-mono text-sm text-neutral-100">{item.name}</span>
        <span className={`text-xs ${statusBadge}`}>{item.status}</span>
      </div>
      {item.arguments && Object.keys(item.arguments).length > 0 && (
        <pre className="mt-2 max-h-40 overflow-auto rounded bg-neutral-950 p-2 font-mono text-xs text-neutral-400">
          {JSON.stringify(item.arguments, null, 2)}
        </pre>
      )}
      {item.status === "completed" && (
        <pre
          className={`mt-2 max-h-60 overflow-auto rounded p-2 font-mono text-xs ${
            item.isError ? "bg-red-950 text-red-200" : "bg-neutral-950 text-neutral-300"
          }`}
        >
          {item.output || (item.isError ? "(error)" : "")}
        </pre>
      )}
      {item.status === "failed" && (
        <p className="mt-2 rounded bg-red-950 p-2 font-mono text-xs text-red-200" data-testid={`tool-error-${item.toolCallId}`}>
          {item.errorKind ? `[${item.errorKind}] ` : ""}
          {item.error}
        </p>
      )}
    </div>
  );
}

export function EventTimeline({ items }: { items: TimelineItem[] }) {
  if (items.length === 0) {
    return <p className="text-sm text-neutral-400">Waiting for events…</p>;
  }
  return (
    <div className="flex flex-col gap-3" aria-label="Run timeline">
      {items.map((item) => {
        if (item.kind === "iteration") {
          return (
            <div
              key={item.id}
              className="flex items-center gap-2 text-xs text-neutral-500"
            >
              <span className="h-px w-6 bg-neutral-700" />
              iteration {item.iteration}
            </div>
          );
        }
        if (item.kind === "tool") {
          return <ToolCard key={item.id} item={item} />;
        }
        return (
          <div
            key={item.id}
            className="whitespace-pre-wrap rounded bg-neutral-900 p-3 text-sm text-neutral-200"
          >
            {item.text}
          </div>
        );
      })}
    </div>
  );
}