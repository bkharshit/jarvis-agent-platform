import { Link } from "react-router";

import { useExecutions } from "@/api/queries/executions";
import { SectionGate } from "@/capabilities/SectionGate";

import { groupSessions } from "./groupSessions";

// Conversations index — derived client-side from GET /v1/executions
// (decision 7). Every row links to the real per-session transcript endpoint.

const PAGE_CAP = 50;

function ConversationsListInner() {
  const { data: runs, isPending, isError, error } = useExecutions({});

  if (isPending) {
    return <p className="px-6 py-10 text-sm text-neutral-400">Loading sessions…</p>;
  }
  if (isError) {
    return <p className="px-6 py-10 text-sm text-red-400">{error.message}</p>;
  }

  const { sessions, total } = groupSessions(runs, PAGE_CAP);

  return (
    <div className="mx-auto max-w-5xl px-6 py-8">
      <h1 className="text-xl font-semibold">Conversations</h1>
      <p className="mt-1 text-sm text-neutral-400">
        Sessions derived from executions — one row per (agent, session).
      </p>

      {sessions.length === 0 ? (
        <p className="mt-10 text-sm text-neutral-400">
          No sessions yet. Run an agent with a session id and it appears here.
        </p>
      ) : (
        <>
          {total > sessions.length && (
            <p className="mt-3 text-xs text-neutral-500">
              Showing the first {sessions.length} of {total} sessions — a proper
              list endpoint comes in a later stage.
            </p>
          )}
          <table className="mt-6 w-full text-left text-sm">
            <thead className="text-neutral-400">
              <tr className="border-b border-neutral-800">
                <th className="py-2 pr-4 font-medium">Agent</th>
                <th className="py-2 pr-4 font-medium">Session</th>
                <th className="py-2 pr-4 font-medium">Runs</th>
                <th className="py-2 pr-4 font-medium">Last activity</th>
              </tr>
            </thead>
            <tbody>
              {sessions.map((s) => (
                <tr key={`${s.agent_id}/${s.session_id}`} className="border-b border-neutral-900">
                  <td className="py-2 pr-4 font-mono text-xs text-neutral-300">
                    {s.agent_id.slice(0, 12)}…
                  </td>
                  <td className="py-2 pr-4">
                    <Link
                      to={`/conversations/${encodeURIComponent(s.agent_id)}/${encodeURIComponent(s.session_id)}`}
                      className="font-mono text-xs text-neutral-100 underline-offset-2 hover:underline"
                    >
                      {s.session_id}
                    </Link>
                  </td>
                  <td className="py-2 pr-4 text-neutral-400">{s.run_count}</td>
                  <td className="py-2 pr-4 text-neutral-400">
                    {s.last_started_at ? new Date(s.last_started_at).toLocaleString() : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </div>
  );
}

export function ConversationsList() {
  return (
    <SectionGate sectionKey="conversations">
      <ConversationsListInner />
    </SectionGate>
  );
}