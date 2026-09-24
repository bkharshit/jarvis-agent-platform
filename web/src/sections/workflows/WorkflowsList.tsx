import { useState } from "react";
import { Link, useNavigate } from "react-router";
import { Play, Plus, Workflow } from "lucide-react";

import { useDeleteWorkflow, useWorkflows } from "@/api/queries/workflows";
import { PageHeader } from "@/components/catalog/PageHeader";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";
import { SectionGate } from "@/capabilities/SectionGate";
import { toast } from "@/stores/toast";

// Workflows list (S6) — real payload rows only, delete with the same
// confirm + conflict-toast shape the agents list uses.

function WorkflowsListInner() {
  const { data: workflows, isPending, isError, error } = useWorkflows();
  const deleteWorkflow = useDeleteWorkflow();
  const navigate = useNavigate();
  const [search, setSearch] = useState("");
  const [view, setView] = useState<"grid" | "table">("grid");

  if (isPending) {
    return <p className="px-6 py-10 text-sm text-neutral-400">Loading workflows…</p>;
  }
  if (isError) {
    return (
      <p role="alert" className="px-6 py-10 text-sm text-red-400">
        {error.message}
      </p>
    );
  }

  const needle = search.trim().toLowerCase();
  const visible =
    needle === ""
      ? workflows
      : workflows.filter(
          (workflow) =>
            workflow.name.toLowerCase().includes(needle) ||
            workflow.description.toLowerCase().includes(needle),
        );

  const onDelete = (id: string, name: string) => {
    if (window.confirm(`Delete workflow ${name}?`)) {
      deleteWorkflow.mutate(id, {
        onError: (e) => toast("error", e instanceof Error ? e.message : String(e)),
      });
    }
  };

  return (
    <div>
      <PageHeader
        title="Workflows"
        description="Author graphs of agent, tool, and condition nodes."
        search={{
          value: search,
          onChange: setSearch,
          placeholder: "Search by name or description…",
        }}
        view={{ value: view, onChange: setView }}
        actions={
          <Button
            icon={<Plus aria-hidden />}
            data-testid="new-workflow"
            onClick={() => void navigate("/workflows/new")}
          >
            New workflow
          </Button>
        }
      />

      {workflows.length === 0 ? (
        <div data-testid="empty-state">
          <EmptyState
            icon={<Workflow className="h-6 w-6" />}
            title="No workflows yet"
            description="Author a graph of agent, tool, and condition nodes — runs reuse the same event model as agents."
            action={
              <Button
                icon={<Plus aria-hidden />}
                data-testid="new-workflow-empty"
                onClick={() => void navigate("/workflows/new")}
              >
                New workflow
              </Button>
            }
          />
        </div>
      ) : visible.length === 0 ? (
        <EmptyState
          icon={<Workflow className="h-6 w-6" />}
          title="No workflows match"
          description="Nothing matches this search — try a different term."
        />
      ) : (
        <ul className="mt-4 flex flex-col gap-2 px-6">
          {visible.map((workflow) => (
            <li key={workflow.id}>
              <Card className="flex items-center justify-between gap-3 p-3 hover:border-neutral-700/80">
                <div className="min-w-0">
                  <Link
                    to={`/workflows/${workflow.id}`}
                    className="text-sm text-neutral-100 underline-offset-2 hover:underline"
                  >
                    {workflow.name}
                  </Link>
                  {workflow.description !== "" && (
                    <p className="truncate text-xs text-neutral-500">{workflow.description}</p>
                  )}
                </div>
                <div className="flex shrink-0 items-center gap-3">
                  <Badge variant="neutral">{workflow.nodes.length} nodes</Badge>
                  <Link
                    to={`/workflows/${workflow.id}/run`}
                    data-testid="run-link-list"
                    className="inline-flex h-7 w-7 items-center justify-center rounded-md text-neutral-400 transition-colors hover:bg-neutral-800 hover:text-neutral-100"
                    aria-label={`Run ${workflow.name}`}
                    title="Run"
                  >
                    <Play aria-hidden className="h-4 w-4" />
                  </Link>
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => onDelete(workflow.id, workflow.name)}
                    className="text-red-400 hover:bg-rose-950/40 hover:text-red-300"
                  >
                    Delete
                  </Button>
                </div>
              </Card>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export function WorkflowsList() {
  return (
    <SectionGate sectionKey="workflows">
      <WorkflowsListInner />
    </SectionGate>
  );
}