import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { client, unwrap } from "@/api/client";
import { agentQueryKey } from "@/api/queries/agents";
import type { components } from "@/api/schema";

type VersionSummary = components["schemas"]["VersionSummary"];

// Versions list from AgentDetail.versions (no versions-list endpoint);
// clicking a version fetches its immutable snapshot into the side panel
// via GET /agents/{id}/versions/{n}.

function useVersionSnapshot(agentId: string, version: number | null) {
  return useQuery({
    queryKey: [...agentQueryKey(agentId), "versions", version],
    enabled: version !== null,
    queryFn: () =>
      unwrap(
        client.GET("/v1/agents/{agent_id}/versions/{version}", {
          params: { path: { agent_id: agentId, version: version! } },
        }),
      ),
  });
}

function SnapshotPanel({ agentId, version }: { agentId: string; version: number }) {
  const { data, isPending, isError, error } = useVersionSnapshot(agentId, version);

  if (isPending) {
    return <p className="text-sm text-neutral-400">Loading version {version}…</p>;
  }
  if (isError) {
    return <p className="text-sm text-red-400">{error.message}</p>;
  }
  const snapshot = data.snapshot;
  return (
    <div className="text-sm">
      <p className="text-neutral-400">
        Snapshot <span className="font-mono text-neutral-200">v{version}</span>
        {data.label ? ` — ${data.label}` : ""} · immutable (D1)
      </p>
      <pre className="mt-3 max-h-96 overflow-auto rounded bg-neutral-900 p-3 font-mono text-xs text-neutral-300">
        {JSON.stringify(snapshot, null, 2)}
      </pre>
    </div>
  );
}

export function VersionsTab({
  agentId,
  versions,
}: {
  agentId: string;
  versions: VersionSummary[];
}) {
  const [selected, setSelected] = useState<number | null>(null);

  if (versions.length === 0) {
    return (
      <p className="text-sm text-neutral-400">
        No versions yet — publishing happens when the agent is first saved.
      </p>
    );
  }

  return (
    <div className="flex gap-6">
      <table className="w-full max-w-sm text-left text-sm">
        <thead className="text-neutral-400">
          <tr className="border-b border-neutral-800">
            <th className="py-2 pr-4 font-medium">Version</th>
            <th className="py-2 pr-4 font-medium">Label</th>
            <th className="py-2 font-medium">Created</th>
          </tr>
        </thead>
        <tbody>
          {versions.map((v) => (
            <tr key={v.version} className="border-b border-neutral-900">
              <td className="py-2 pr-4">
                <button
                  type="button"
                  onClick={() => setSelected(v.version)}
                  className={`cursor-pointer font-mono underline-offset-2 hover:underline ${
                    selected === v.version ? "text-neutral-100 underline" : "text-neutral-300"
                  }`}
                >
                  v{v.version}
                </button>
              </td>
              <td className="py-2 pr-4 text-neutral-400">{v.label || "—"}</td>
              <td className="py-2 text-neutral-400">
                {new Date(v.created_at).toLocaleString()}
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      <div className="min-w-0 flex-1">
        {selected === null ? (
          <p className="text-sm text-neutral-400">Select a version to view its snapshot.</p>
        ) : (
          <SnapshotPanel agentId={agentId} version={selected} />
        )}
      </div>
    </div>
  );
}