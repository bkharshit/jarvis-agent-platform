import { useState } from "react";

import { useAgents } from "@/api/queries/agents";
import { useEvalCompare } from "@/api/queries/evaluations";

// Version comparison (S11, ADR 0017 §6) — "a query, not a feature":
// eval runs + results aggregated per agent_version_id for one agent,
// pass-rate and per-scorer means side by side.

export function EvalCompare() {
  const { data: agents } = useAgents();
  const [agentId, setAgentId] = useState("");
  const { data: comparison, isPending, isError, error } = useEvalCompare(agentId || undefined);

  return (
    <div className="mx-auto max-w-4xl px-6 py-8">
      <h1 className="text-xl font-semibold text-neutral-100">Compare versions</h1>
      <p className="mt-1 text-sm text-neutral-400">
        Scored eval results grouped by the agent version each run pinned.
      </p>
      <select
        value={agentId}
        onChange={(event) => setAgentId(event.target.value)}
        className="mt-4 rounded border border-neutral-700 bg-neutral-800 px-2 py-1 text-sm text-neutral-100"
      >
        <option value="">Pick an agent…</option>
        {(agents ?? []).map((agent) => (
          <option key={agent.id} value={agent.id}>
            {agent.name}
          </option>
        ))}
      </select>

      {!agentId ? null : isPending ? (
        <p className="mt-6 text-sm text-neutral-400">Aggregating…</p>
      ) : isError ? (
        <p className="mt-6 text-sm text-red-400">{error.message}</p>
      ) : comparison.versions.length === 0 ? (
        <p className="mt-6 text-sm text-neutral-400">
          No eval runs for this agent yet.
        </p>
      ) : (
        <table className="mt-6 w-full text-left text-sm">
          <thead className="text-neutral-400">
            <tr className="border-b border-neutral-800">
              <th className="py-2 pr-4 font-medium">Version</th>
              <th className="py-2 pr-4 font-medium">Runs</th>
              <th className="py-2 pr-4 font-medium">Cases</th>
              <th className="py-2 pr-4 font-medium">Scored</th>
              <th className="py-2 pr-4 font-medium">Pass rate</th>
              <th className="py-2 font-medium">Per scorer</th>
            </tr>
          </thead>
          <tbody>
            {comparison.versions.map((version) => (
              <tr key={version.agent_version_id} className="border-b border-neutral-900 align-top">
                <td className="py-2 pr-4 font-mono text-xs text-neutral-200">
                  {version.agent_version_id.slice(0, 8)}
                </td>
                <td className="py-2 pr-4 text-neutral-300">{version.runs}</td>
                <td className="py-2 pr-4 text-neutral-300">{version.cases}</td>
                <td className="py-2 pr-4 text-neutral-300">{version.scored}</td>
                <td className="py-2 pr-4 text-neutral-300">
                  {version.pass_rate == null ? "—" : `${Math.round(version.pass_rate * 100)}%`}
                </td>
                <td className="py-2">
                  <div className="flex flex-wrap gap-1">
                    {version.scorers.map((scorer) => (
                      <span
                        key={scorer.scorer}
                        className="rounded bg-neutral-800 px-2 py-0.5 font-mono text-xs text-neutral-300"
                      >
                        {scorer.scorer}: {scorer.passed}/{scorer.scored}
                        {scorer.mean != null ? ` (mean ${scorer.mean.toFixed(2)})` : ""}
                      </span>
                    ))}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}