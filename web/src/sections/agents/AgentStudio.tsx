import { useEffect, useState } from "react";
import { Link, useParams } from "react-router";

import { useAgent } from "@/api/queries/agents";
import { SectionGate } from "@/capabilities/SectionGate";
import { useEditorStore } from "@/stores/editorStore";

import { AgentConfigPane } from "./AgentConfigPane";
import { RunDock } from "./RunDock";
import { VersionsTab } from "./VersionsTab";

// Agent Studio (commit 7 of the modernization): the editor and the run
// console merged into one split screen — left 55% configuration (the
// editor IS the definition; the old definition tab is gone), right 45%
// the live run dock. Versions/Runs/API absorb into the left pane's tabs.
// The old /edit and /run routes redirect here (routes.tsx).

type TabKey = "configure" | "versions" | "runs" | "api";

const TABS: { key: TabKey; label: string }[] = [
  { key: "configure", label: "Configure" },
  { key: "versions", label: "Versions" },
  { key: "runs", label: "Runs" },
  { key: "api", label: "API access" },
];

function RunsTab({ agentId }: { agentId: string }) {
  return (
    <div className="flex flex-col gap-3 text-sm">
      <p className="text-neutral-400">Executions of this agent:</p>
      <div className="flex gap-3">
        <Link
          to={`/executions?agent=${agentId}`}
          className="rounded border border-neutral-700 px-3 py-1.5 text-neutral-200 hover:bg-neutral-900"
        >
          View runs →
        </Link>
        <Link
          to={`/agents/${agentId}`}
          className="rounded bg-neutral-100 px-3 py-1.5 font-medium text-neutral-900 hover:bg-white"
        >
          Run this agent
        </Link>
      </div>
    </div>
  );
}

function ApiTab({ agentId }: { agentId: string }) {
  const curl = `curl -X POST http://localhost:8000/v1/agents/${agentId}/run \\
  -H 'Content-Type: application/json' \\
  -d '{"input": "your input here"}'`;
  return (
    <div className="text-sm">
      <p className="text-neutral-400">Run this agent from the command line:</p>
      <pre className="mt-3 overflow-x-auto rounded bg-neutral-900 p-3 font-mono text-xs text-neutral-300">
        {curl}
      </pre>
      <p className="mt-3 text-xs text-neutral-500">
        Streaming: POST the same body to /v1/agents/{agentId.slice(0, 8)}…/stream and read the SSE
        frames. Secrets are env-var names — no keys in the payload.
      </p>
    </div>
  );
}

function AgentStudioInner() {
  const { agentId } = useParams();
  const [tab, setTab] = useState<TabKey>("configure");
  const { data: detail, isPending, isError, error } = useAgent(agentId);
  const close = useEditorStore((s) => s.close);

  // Draft lifecycle lives at the STUDIO level: the config pane unmounts on
  // every tab switch, so a pane-level unmount-close would wipe an in-progress
  // edit. The Studio itself unmounts only when the route leaves the agent.
  useEffect(() => close, [close]);

  if (agentId !== undefined && isPending) {
    return <p className="px-6 py-10 text-sm text-neutral-400">Loading agent…</p>;
  }
  if (agentId !== undefined && isError) {
    return (
      <div className="px-6 py-10">
        <p className="text-sm text-red-400">{error.message}</p>
        <Link to="/agents" className="mt-4 inline-block text-sm text-neutral-300 underline">
          Back to agents
        </Link>
      </div>
    );
  }

  return (
    <div className="flex h-[calc(100vh-3.5rem)]">
      <div className="w-[55%] min-w-0 overflow-y-auto border-r border-neutral-800">
        <div className="px-6 py-8">
          <div className="flex items-center gap-3">
            <h1 className="text-xl font-semibold">
              {agentId === undefined
                ? "New agent"
                : (detail?.definition.name ?? "Agent")}
            </h1>
            {agentId !== undefined && detail && (
              <span className="rounded-full bg-neutral-800 px-2 py-0.5 text-xs text-neutral-400">
                {detail.versions.length} version{detail.versions.length === 1 ? "" : "s"}
              </span>
            )}
          </div>

          {agentId !== undefined && (
            <div role="tablist" aria-label="Agent detail" className="mt-4 flex gap-1 border-b border-neutral-800">
              {TABS.map(({ key, label }) => (
                <button
                  key={key}
                  type="button"
                  role="tab"
                  aria-selected={tab === key}
                  onClick={() => setTab(key)}
                  className={`cursor-pointer px-3 py-2 text-sm ${
                    tab === key
                      ? "border-b-2 border-neutral-100 text-neutral-100"
                      : "text-neutral-400 hover:text-neutral-200"
                  }`}
                >
                  {label}
                </button>
              ))}
            </div>
          )}

          <div className="py-6">
            {tab === "configure" && <AgentConfigPane />}
            {tab === "versions" && detail && (
              <VersionsTab agentId={agentId!} versions={detail.versions} />
            )}
            {tab === "runs" && agentId !== undefined && <RunsTab agentId={agentId} />}
            {tab === "api" && agentId !== undefined && <ApiTab agentId={agentId} />}
          </div>
        </div>
      </div>
      <div className="w-[45%] min-w-0 overflow-y-auto">
        <RunDock />
      </div>
    </div>
  );
}

export function AgentStudio() {
  return (
    <SectionGate sectionKey="agents">
      <AgentStudioInner />
    </SectionGate>
  );
}
