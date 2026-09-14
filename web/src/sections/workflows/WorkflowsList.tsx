import { Link, useNavigate } from "react-router";

import { useDeleteWorkflow, useWorkflows } from "@/api/queries/workflows";
import { SectionGate } from "@/capabilities/SectionGate";
import { toast } from "@/stores/toast";

// Workflows list (S6) — real payload rows only, delete with the same
// confirm + conflict-toast shape the agents list uses.

function WorkflowsListInner() {
  const { data: workflows, isPending, isError, error } = useWorkflows();
  const deleteWorkflow = useDeleteWorkflow();
  const navigate = useNavigate();

  if (isPending) {
    return <p className="px-6 py-10 text-sm text-neutral-400">Loading workflows…</p>;
  }
  if (isError) {
    return <p className="px-6 py-10 text-sm text-red-400">{error.message}</p>;
  }

  return (
    <div className="mx-auto max-w-4xl px-6 py-8">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">Workflows</h1>
        <button
          type="button"
          data-testid="new-workflow"
          onClick={() => void navigate("/workflows/new")}
          className="cursor-pointer rounded bg-neutral-100 px-4 py-1.5 text-sm font-medium text-neutral-900 hover:bg-white"
        >
          New workflow
        </button>
      </div>

      {workflows.length === 0 ? (
        <p className="mt-6 text-sm text-neutral-400" data-testid="empty-state">
          No workflows yet — author a graph of agent, tool, and condition nodes.
        </p>
      ) : (
        <ul className="mt-4 flex flex-col gap-2">
          {workflows.map((workflow) => (
            <li
              key={workflow.id}
              className="flex items-center justify-between rounded border border-neutral-800 bg-neutral-900 p-3"
            >
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
              <div className="flex items-center gap-3">
                <span className="text-xs text-neutral-500">
                  {workflow.nodes.length} nodes
                </span>
                <Link
                  to={`/workflows/${workflow.id}/run`}
                  className="text-xs text-neutral-200 underline"
                >
                  Run
                </Link>
                <button
                  type="button"
                  onClick={() => {
                    if (window.confirm(`Delete workflow ${workflow.name}?`)) {
                      deleteWorkflow.mutate(workflow.id, {
                        onError: (e) => toast("error", e instanceof Error ? e.message : String(e)),
                      });
                    }
                  }}
                  className="cursor-pointer text-xs text-red-300 hover:underline"
                >
                  Delete
                </button>
              </div>
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