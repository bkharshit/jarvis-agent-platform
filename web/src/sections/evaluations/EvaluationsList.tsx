import { Link, useNavigate } from "react-router";
import { useState } from "react";

import {
  useCreateEvalDataset,
  useDeleteEvalDataset,
  useEvalDatasets,
} from "@/api/queries/evaluations";
import { Button } from "@/components/ui/Button";
import { EmptyState } from "@/components/ui/EmptyState";
import { Field } from "@/components/ui/Field";
import { Input } from "@/components/ui/Input";
import { toast } from "@/stores/toast";

// Evaluations list (S11, ADR 0017 §6): the tenant's datasets. Creation
// starts a small starter dataset (one case, exact scorer) that the
// dataset editor fills in — the backend owns validation (422).

function CreateDatasetForm({
  onCreated,
  onCancelled,
}: {
  onCreated: (datasetId: string) => void;
  onCancelled: () => void;
}) {
  const create = useCreateEvalDataset();
  const [name, setName] = useState("");
  const [firstInput, setFirstInput] = useState("");

  return (
    <form
      className="mt-4 rounded-xl border border-neutral-800/80 bg-neutral-900/50 p-4 shadow-sm"
      onSubmit={(event) => {
        event.preventDefault();
        create.mutate(
          {
            name,
            description: "",
            // an empty case input is a 422 at the boundary (RunRequest.input
            // is min_length=1) — the first case's input is collected here
            cases: [{ id: "c-1", input: firstInput.trim(), expected: "" }],
            scorers: [{ name: "exact" }],
          },
          {
            onSuccess: (dataset) => onCreated(dataset.id),
            onError: (err) => toast("error", err.message),
          },
        );
      }}
    >
      <Field label="Dataset name" htmlFor="dataset-name" className="text-sm">
        <Input
          id="dataset-name"
          required
          value={name}
          onChange={(event) => setName(event.target.value)}
          placeholder="smoke-dataset"
        />
      </Field>
      <Field
        label="First case input"
        htmlFor="dataset-first-input"
        className="mt-3 text-sm"
      >
        <Input
          id="dataset-first-input"
          required
          value={firstInput}
          onChange={(event) => setFirstInput(event.target.value)}
          placeholder="what to ask the agent…"
        />
      </Field>
      <div className="mt-3 flex gap-2">
        <Button
          type="submit"
          disabled={create.isPending || !name.trim() || !firstInput.trim()}
        >
          {create.isPending ? "Creating…" : "Create dataset"}
        </Button>
        <Button variant="secondary" onClick={onCancelled}>
          Cancel
        </Button>
      </div>
    </form>
  );
}

export function EvaluationsList() {
  const navigate = useNavigate();
  const [creating, setCreating] = useState(false);
  const deleteDataset = useDeleteEvalDataset();
  const { data: datasets, isPending, isError, error } = useEvalDatasets();

  if (isPending) {
    return <p className="px-6 py-10 text-sm text-neutral-400">Loading datasets…</p>;
  }
  if (isError) {
    return (
      <div className="px-6 py-10">
        <p role="alert" className="text-sm text-red-400">{error.message}</p>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-4xl px-6 py-8">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">Evaluations</h1>
        <Button onClick={() => setCreating(true)}>New dataset</Button>
      </div>
      <p className="mt-1 text-sm text-neutral-400">
        Test-case datasets scored against your agents&apos; published versions.
      </p>

      {creating && (
        <CreateDatasetForm
          onCreated={(datasetId) => {
            setCreating(false);
            navigate(`/evaluations/datasets/${datasetId}`);
          }}
          onCancelled={() => setCreating(false)}
        />
      )}

      {datasets.length === 0 ? (
        <EmptyState
          title="No datasets yet"
          description="Create one to get started."
        />
      ) : (
        <table className="mt-6 w-full text-left text-sm">
          <thead className="text-neutral-400">
            <tr className="border-b border-neutral-800">
              <th className="py-2 pr-4 font-medium">Name</th>
              <th className="py-2 pr-4 font-medium">Cases</th>
              <th className="py-2 pr-4 font-medium">Scorers</th>
              <th className="py-2 pr-4 text-right font-medium">Actions</th>
            </tr>
          </thead>
          <tbody>
            {datasets.map((dataset) => (
              <tr key={dataset.id} className="border-b border-neutral-900">
                <td className="py-2 pr-4">
                  <Link
                    to={`/evaluations/datasets/${dataset.id}`}
                    className="text-neutral-100 underline-offset-2 hover:underline"
                  >
                    {dataset.name}
                  </Link>
                </td>
                <td className="py-2 pr-4 text-neutral-300">{dataset.cases.length}</td>
                <td className="py-2 pr-4 font-mono text-xs text-neutral-300">
                  {dataset.scorers.map((scorer) => scorer.name).join(", ")}
                </td>
                <td className="py-2 pr-4 text-right">
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => {
                      if (!window.confirm(`Delete dataset "${dataset.name}"?`)) return;
                      deleteDataset.mutate(dataset.id, {
                        onError: (err) => toast("error", err.message),
                      });
                    }}
                    className="text-red-400 hover:bg-rose-950/40 hover:text-red-300"
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