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

/** S6 (D43): one workflow node's inner events, grouped under its label. */
function NodeGroup({ item }: { item: Extract<TimelineItem, { kind: "node" }> }) {
  const badge = {
    running: "text-amber-300",
    completed: "text-green-300",
    failed: "text-red-300",
  }[item.status];
  return (
    <div
      className="rounded border border-neutral-800 bg-neutral-900/40 p-3"
      data-testid={`node-group-${item.nodeId}`}
    >
      <div className="flex items-center justify-between">
        <span className="font-mono text-sm text-neutral-100">{item.nodeId}</span>
        <span className={`text-xs ${badge}`}>
          {item.nodeType}
          {item.status !== "running" ? ` · ${item.status}` : ""}
        </span>
      </div>
      {item.items.length > 0 ? (
        <div className="mt-2 flex flex-col gap-2 border-l border-neutral-800 pl-3">
          {item.items.map((child) => {
            if (child.kind === "iteration") {
              return (
                <div key={child.id} className="flex items-center gap-2 text-xs text-neutral-500">
                  <span className="h-px w-6 bg-neutral-700" />
                  iteration {child.iteration}
                </div>
              );
            }
            if (child.kind === "tool") return <ToolCard key={child.id} item={child} />;
            if (child.kind === "node") return <NodeGroup key={child.id} item={child} />;
            return (
              <div
                key={child.id}
                className="whitespace-pre-wrap rounded bg-neutral-950 p-3 text-sm text-neutral-200"
              >
                {child.text}
              </div>
            );
          })}
        </div>
      ) : null}
      {item.status === "completed" && item.output !== undefined && (
        <p className="mt-2 truncate text-xs text-neutral-500">→ {item.output}</p>
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
        if (item.kind === "node") {
          return <NodeGroup key={item.id} item={item} />;
        }
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