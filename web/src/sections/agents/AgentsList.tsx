import { Link, useNavigate } from "react-router";

import { useAgents, useDeleteAgent } from "@/api/queries/agents";
import { toast } from "@/stores/toast";

// Agents list — the real backend payload or an honest empty state. No
// mocked rows, ever (decision 1.6).

export function AgentsList() {
  const navigate = useNavigate();
  const { data: agents, isPending, isError, error } = useAgents();
  const deleteAgent = useDeleteAgent();

  if (isPending) {
    return <p className="px-6 py-10 text-sm text-neutral-400">Loading agents…</p>;
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
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">Agents</h1>
        <button
          type="button"
          onClick={() => navigate("/agents/new")}
          className="cursor-pointer rounded bg-neutral-100 px-3 py-1.5 text-sm font-medium text-neutral-900 hover:bg-white"
        >
          New agent
        </button>
      </div>

      {agents.length === 0 ? (
        <p className="mt-10 text-sm text-neutral-400">
          No agents yet. Create one to get started.
        </p>
      ) : (
        <table className="mt-6 w-full text-left text-sm">
          <thead className="text-neutral-400">
            <tr className="border-b border-neutral-800">
              <th className="py-2 pr-4 font-medium">Name</th>
              <th className="py-2 pr-4 font-medium">Model</th>
              <th className="py-2 pr-4 font-medium">Strategy</th>
              <th className="py-2 pr-4 text-right font-medium">Actions</th>
            </tr>
          </thead>
          <tbody>
            {agents.map((agent) => (
              <tr key={agent.id} className="border-b border-neutral-900">
                <td className="py-2 pr-4">
                  <Link
                    to={`/agents/${agent.id}`}
                    className="text-neutral-100 underline-offset-2 hover:underline"
                  >
                    {agent.name}
                  </Link>
                </td>
                <td className="py-2 pr-4 text-neutral-400">
                  {agent.model.provider}/{agent.model.model}
                </td>
                <td className="py-2 pr-4 text-neutral-400">{agent.strategy.type}</td>
                <td className="py-2 pr-4 text-right">
                  <button
                    type="button"
                    disabled={deleteAgent.isPending}
                    onClick={() => {
                      if (!window.confirm(`Delete agent "${agent.name}"? This cannot be undone.`)) {
                        return;
                      }
                      deleteAgent.mutate(agent.id, {
                        onError: (err) => toast("error", err.message),
                      });
                    }}
                    className="cursor-pointer text-red-400 hover:text-red-300 disabled:cursor-not-allowed disabled:text-neutral-600"
                  >
                    Delete
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}