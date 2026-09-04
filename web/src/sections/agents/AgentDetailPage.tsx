import { useState } from "react";
import { Link, useParams } from "react-router";

import { useAgent, type AgentDefinition } from "@/api/queries/agents";
import { SectionGate } from "@/capabilities/SectionGate";

import { VersionsTab } from "./VersionsTab";

// Detail tabs (decision 10): Definition · Versions · Runs · API access.
// Versions lists from AgentDetail.versions — there is no versions-list
// endpoint; snapshots load on demand from /versions/{n}.

type TabKey = "definition" | "versions" | "runs" | "api";

const TABS: { key: TabKey; label: string }[] = [
  { key: "definition", label: "Definition" },
  { key: "versions", label: "Versions" },
  { key: "runs", label: "Runs" },
  { key: "api", label: "API access" },
];

function DefinitionTab({ definition }: { definition: AgentDefinition }) {
  return (
    <div className="flex flex-col gap-3 text-sm">
      <div>
        <span className="text-neutral-400">Name: </span>
        <span className="text-neutral-100">{definition.name}</span>
      </div>
      <div>
        <span className="text-neutral-400">Description: </span>
        <span className="text-neutral-100">{definition.description || "—"}</span>
      </div>
      <div>
        <span className="text-neutral-400">Model: </span>
        <span className="font-mono text-neutral-100">
          {definition.model.provider}/{definition.model.model}
          {definition.model.base_url ? ` @ ${definition.model.base_url}` : ""}
        </span>
      </div>
      <div>
        <span className="text-neutral-400">Strategy: </span>
        <span className="font-mono text-neutral-100">{definition.strategy.type}</span>
      </div>
      <div>
        <span className="text-neutral-400">Tools: </span>
        <span className="font-mono text-neutral-100">
          {(definition.tools ?? []).length > 0
            ? (definition.tools ?? [])
                .map((t) => `${t.name}${t.enabled ? "" : " (disabled)"}`)
                .join(", ")
            : "none"}
        </span>
      </div>
      <div>
        <span className="text-neutral-400">Memory: </span>
        <span className="font-mono text-neutral-100">
          {definition.memory?.enabled
            ? `enabled, max ${definition.memory.max_messages} messages`
            : "disabled"}
        </span>
      </div>
      <div>
        <span className="text-neutral-400">Limits: </span>
        <span className="font-mono text-neutral-100">
          max_iterations={definition.max_iterations}, temperature={definition.temperature}
        </span>
      </div>
      <div>
        <span className="text-neutral-400">System prompt: </span>
        <pre className="mt-1 whitespace-pre-wrap rounded bg-neutral-900 p-3 text-xs text-neutral-300">
          {definition.system_prompt || "—"}
        </pre>
      </div>
      <Link
        to={`/agents/${definition.id}/edit`}
        className="mt-2 w-fit rounded border border-neutral-700 px-3 py-1.5 text-neutral-200 hover:bg-neutral-900"
      >
        Edit agent
      </Link>
    </div>
  );
}

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
          to={`/agents/${agentId}/run`}
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

function AgentDetailInner() {
  const { agentId } = useParams();
  const [tab, setTab] = useState<TabKey>("definition");
  const { data: detail, isPending, isError, error } = useAgent(agentId);

  if (isPending) {
    return <p className="px-6 py-10 text-sm text-neutral-400">Loading agent…</p>;
  }
  if (isError) {
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
    <div className="mx-auto max-w-4xl px-6 py-8">
      <div className="flex items-center gap-3">
        <h1 className="text-xl font-semibold">{detail.definition.name}</h1>
        <span className="rounded-full bg-neutral-800 px-2 py-0.5 text-xs text-neutral-400">
          {detail.versions.length} version{detail.versions.length === 1 ? "" : "s"}
        </span>
      </div>

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

      <div className="py-6">
        {tab === "definition" && <DefinitionTab definition={detail.definition} />}
        {tab === "versions" && <VersionsTab agentId={agentId!} versions={detail.versions} />}
        {tab === "runs" && <RunsTab agentId={agentId!} />}
        {tab === "api" && <ApiTab agentId={agentId!} />}
      </div>
    </div>
  );
}

export function AgentDetailPage() {
  return (
    <SectionGate sectionKey="agents">
      <AgentDetailInner />
    </SectionGate>
  );
}