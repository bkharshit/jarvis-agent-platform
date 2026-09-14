import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router";

import {
  getWorkflow,
  useCreateWorkflow,
  useUpdateWorkflow,
  useWorkflow,
  type WorkflowDefinition,
} from "@/api/queries/workflows";
import { SectionGate } from "@/capabilities/SectionGate";
import { hashDefinition, definitionToGraph, graphToPayload, type CanvasGraph } from "./graphState";
import { WorkflowCanvas } from "./WorkflowCanvas";

// Workflow editor (S6): the canvas plus draft-save. Saves go through the
// server hash — before every PATCH the definition is re-fetched and hashed;
// a change since this editor loaded is a conflict surfaced to the user, not
// a silent clobber. Create publishes v1; PATCH publishes the next version
// (both re-pin agent nodes server-side, D42).

function EditorInner() {
  const { workflowId } = useParams();
  const navigate = useNavigate();
  const isNew = workflowId === undefined;
  const { data: detail, isPending, isError, error } = useWorkflow(isNew ? undefined : workflowId);

  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [maxNodeExecutions, setMaxNodeExecutions] = useState(24);
  const [graph, setGraph] = useState<CanvasGraph>({ nodes: [], edges: [], startNodeId: "" });
  /** Hash of the definition as this editor loaded it — the conflict guard. */
  const [serverHash, setServerHash] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const createWorkflow = useCreateWorkflow();
  const updateWorkflow = useUpdateWorkflow(workflowId ?? "");

  useEffect(() => {
    if (detail === undefined) return;
    const { definition } = detail;
    setName(definition.name);
    setDescription(definition.description);
    setMaxNodeExecutions(definition.max_node_executions);
    setGraph(definitionToGraph(definition));
    setServerHash(hashDefinition(definition));
  }, [detail]);

  if (!isNew && isPending) {
    return <p className="px-6 py-10 text-sm text-neutral-400">Loading workflow…</p>;
  }
  if (!isNew && isError) {
    return (
      <p className="px-6 py-10 text-sm text-red-400">{error.message}</p>
    );
  }

  async function save() {
    if (graph.nodes.length === 0) {
      setSaveError("add at least one node");
      return;
    }
    if (graph.startNodeId === "" || !graph.nodes.some((n) => n.id === graph.startNodeId)) {
      setSaveError("pick a start node");
      return;
    }
    if (name.trim() === "") {
      setSaveError("name must not be blank");
      return;
    }
    setSaving(true);
    setSaveError(null);
    try {
      if (isNew) {
        const detail = await createWorkflow.mutateAsync(
          graphToPayload(graph, { name, description, maxNodeExecutions }) as Parameters<
            typeof createWorkflow.mutateAsync
          >[0],
        );
        void navigate(`/workflows/${detail.definition.id}`);
      } else {
        // Server-hash guard: a definition changed since this editor loaded
        // is a conflict — reload, never a silent overwrite.
        const current = await getWorkflow(workflowId!);
        if (hashDefinition(current) !== serverHash) {
          setSaveError(
            "the workflow changed on the server since you opened it — reload to pick up the current version",
          );
          return;
        }
        const next = await updateWorkflow.mutateAsync(
          graphToPayload(graph, { name, description, maxNodeExecutions }) as Parameters<
            typeof updateWorkflow.mutateAsync
          >[0],
        );
        setServerHash(hashDefinition(next.definition));
      }
    } catch (e) {
      setSaveError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="mx-auto max-w-5xl px-6 py-8">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">{isNew ? "New workflow" : "Edit workflow"}</h1>
        <div className="flex items-center gap-3">
          <Link to="/workflows" className="text-sm text-neutral-400 underline">
            All workflows
          </Link>
          {!isNew && (
            <Link
              to={`/workflows/${workflowId}/run`}
              className="text-sm text-neutral-200 underline"
              data-testid="run-link"
            >
              Run
            </Link>
          )}
        </div>
      </div>

      <div className="mt-4 flex flex-wrap items-center gap-3">
        <input
          aria-label="Workflow name"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="workflow name"
          className="w-64 rounded border border-neutral-700 bg-neutral-900 px-2 py-1.5 text-sm text-neutral-100"
        />
        <input
          aria-label="Workflow description"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          placeholder="description (optional)"
          className="w-72 rounded border border-neutral-700 bg-neutral-900 px-2 py-1.5 text-sm text-neutral-100"
        />
        <label className="flex items-center gap-2 text-xs text-neutral-400">
          max node executions
          <input
            type="number"
            min={1}
            max={128}
            aria-label="Max node executions"
            value={maxNodeExecutions}
            onChange={(e) => setMaxNodeExecutions(Number(e.target.value))}
            className="w-20 rounded border border-neutral-700 bg-neutral-900 px-2 py-1 text-sm text-neutral-100"
          />
        </label>
        <button
          type="button"
          data-testid="save-workflow"
          onClick={() => void save()}
          disabled={saving}
          className="cursor-pointer rounded bg-neutral-100 px-4 py-1.5 text-sm font-medium text-neutral-900 hover:bg-white disabled:cursor-not-allowed disabled:text-neutral-500"
        >
          {saving ? "Saving…" : isNew ? "Create & publish" : "Save & publish"}
        </button>
      </div>

      {saveError !== null && (
        <p className="mt-2 text-sm text-red-400" role="alert" data-testid="save-error">
          {saveError}
        </p>
      )}

      <div className="mt-4">
        <WorkflowCanvas graph={graph} onChange={setGraph} />
      </div>
    </div>
  );
}

export function WorkflowEditorPage() {
  return (
    <SectionGate sectionKey="workflows">
      <EditorInner />
    </SectionGate>
  );
}

// Keep the schema type referenced for consumers building on the editor.
export type { WorkflowDefinition };