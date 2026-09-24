import { useState } from "react";
import { Link, useNavigate } from "react-router";
import { Bot, Pencil, Play, Plus } from "lucide-react";

import { useAgents, useDeleteAgent } from "@/api/queries/agents";
import { PageHeader } from "@/components/catalog/PageHeader";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";
import { toast } from "@/stores/toast";

// Agents list — the real backend payload or an honest empty state. No
// mocked rows, ever (decision 1.6).

export function AgentsList() {
  const navigate = useNavigate();
  const { data: agents, isPending, isError, error } = useAgents();
  const deleteAgent = useDeleteAgent();
  const [search, setSearch] = useState("");
  const [view, setView] = useState<"grid" | "table">("grid");

  if (isPending) {
    return <p className="px-6 py-10 text-sm text-neutral-400">Loading agents…</p>;
  }
  if (isError) {
    return (
      <div className="px-6 py-10">
        <p role="alert" className="text-sm text-red-400">
          {error.message}
        </p>
      </div>
    );
  }

  const needle = search.trim().toLowerCase();
  const visible =
    needle === ""
      ? agents
      : agents.filter(
          (agent) =>
            agent.name.toLowerCase().includes(needle) ||
            (agent.description ?? "").toLowerCase().includes(needle),
        );

  const onDelete = (id: string, name: string) => {
    if (!window.confirm(`Delete agent "${name}"? This cannot be undone.`)) {
      return;
    }
    deleteAgent.mutate(id, {
      onError: (err) => toast("error", err.message),
    });
  };

  return (
    <div>
      <PageHeader
        title="Agents"
        description="Build, configure, and debug agents in the Studio."
        search={{
          value: search,
          onChange: setSearch,
          placeholder: "Search by name or description…",
        }}
        view={{ value: view, onChange: setView }}
        actions={
          <Button icon={<Plus aria-hidden />} onClick={() => navigate("/agents/new")}>
            New agent
          </Button>
        }
      />

      {agents.length === 0 ? (
        <EmptyState
          icon={<Bot className="h-6 w-6" />}
          title="No agents yet"
          description="Create your first agent to get started — pick a model, attach tools, and debug it live in the Studio."
          action={
            <Button icon={<Plus aria-hidden />} onClick={() => navigate("/agents/new")}>
              New agent
            </Button>
          }
        />
      ) : visible.length === 0 ? (
        <EmptyState
          icon={<Bot className="h-6 w-6" />}
          title="No agents match"
          description="Nothing matches this search — try a different term."
        />
      ) : view === "grid" ? (
        <ul className="grid grid-cols-1 gap-4 px-6 py-6 md:grid-cols-2 xl:grid-cols-3">
          {visible.map((agent) => (
            <li key={agent.id}>
              <Card className="group flex h-full flex-col gap-3 p-4 hover:border-neutral-700/80">
                <div className="flex items-start justify-between gap-2">
                  <div className="flex min-w-0 items-center gap-2.5">
                    <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-indigo-500/10 text-indigo-300">
                      <Bot aria-hidden className="h-5 w-5" />
                    </span>
                    <Link
                      to={`/agents/${agent.id}`}
                      className="min-w-0 truncate text-sm font-medium text-neutral-100 underline-offset-2 hover:underline"
                    >
                      {agent.name}
                    </Link>
                  </div>
                  <Badge variant="brand">{agent.strategy.type}</Badge>
                </div>
                <p className="line-clamp-2 min-h-8 text-xs text-neutral-500">
                  {agent.description || "No description."}
                </p>
                <div className="mt-auto flex items-center justify-between gap-2 border-t border-neutral-800/80 pt-3">
                  <Badge variant="neutral">
                    {agent.model.provider}/{agent.model.model}
                  </Badge>
                  <div className="flex items-center gap-1">
                    <Link
                      to={`/agents/${agent.id}`}
                      aria-label={`Run ${agent.name}`}
                      title="Open in Studio"
                      className="inline-flex h-7 w-7 items-center justify-center rounded-md text-neutral-400 transition-colors hover:bg-neutral-800 hover:text-neutral-100"
                    >
                      <Play aria-hidden className="h-4 w-4" />
                    </Link>
                    <Link
                      to={`/agents/${agent.id}`}
                      aria-label={`Edit ${agent.name}`}
                      title="Configure"
                      className="inline-flex h-7 w-7 items-center justify-center rounded-md text-neutral-400 transition-colors hover:bg-neutral-800 hover:text-neutral-100"
                    >
                      <Pencil aria-hidden className="h-4 w-4" />
                    </Link>
                    <Button
                      variant="ghost"
                      size="sm"
                      disabled={deleteAgent.isPending}
                      onClick={() => onDelete(agent.id, agent.name)}
                      className="text-red-400 hover:bg-rose-950/40 hover:text-red-300"
                    >
                      Delete
                    </Button>
                  </div>
                </div>
              </Card>
            </li>
          ))}
        </ul>
      ) : (
        <table className="w-full px-6 text-left text-sm">
          <thead className="text-neutral-400">
            <tr className="border-b border-neutral-800">
              <th className="px-6 py-2 pr-4 font-medium">Name</th>
              <th className="py-2 pr-4 font-medium">Model</th>
              <th className="py-2 pr-4 font-medium">Strategy</th>
              <th className="py-2 pr-6 text-right font-medium">Actions</th>
            </tr>
          </thead>
          <tbody>
            {visible.map((agent) => (
              <tr key={agent.id} className="border-b border-neutral-900">
                <td className="py-2 pr-4 pl-6">
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
                <td className="py-2 pr-6 text-right">
                  <Button
                    variant="ghost"
                    size="sm"
                    disabled={deleteAgent.isPending}
                    onClick={() => onDelete(agent.id, agent.name)}
                    className="text-red-400 hover:text-red-300"
                  >
                    Delete
                  </Button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}