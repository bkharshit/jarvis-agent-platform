import { Link, useNavigate } from "react-router";
import { useState } from "react";

import {
  useCreateEvalDataset,
  useDeleteEvalDataset,
  useEvalDatasets,
} from "@/api/queries/evaluations";
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
      className="mt-4 rounded border border-neutral-800 bg-neutral-900 p-4"
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
      <label className="block text-sm text-neutral-300" htmlFor="dataset-name">
        Dataset name
      </label>
      <input
        id="dataset-name"
        required
        value={name}
        onChange={(event) => setName(event.target.value)}
        placeholder="smoke-dataset"
        className="mt-1 w-full rounded border border-neutral-700 bg-neutral-800 px-2 py-1 text-sm text-neutral-100"
      />
      <label className="mt-3 block text-sm text-neutral-300" htmlFor="dataset-first-input">
        First case input
      </label>
      <input
        id="dataset-first-input"
        required
        value={firstInput}
        onChange={(event) => setFirstInput(event.target.value)}
        placeholder="what to ask the agent…"
        className="mt-1 w-full rounded border border-neutral-700 bg-neutral-800 px-2 py-1 text-sm text-neutral-100"
      />
      <div className="mt-3 flex gap-2">
        <button
          type="submit"
          disabled={create.isPending || !name.trim() || !firstInput.trim()}
          className="cursor-pointer rounded bg-neutral-100 px-3 py-1.5 text-sm font-medium text-neutral-900 hover:bg-white disabled:opacity-50"
        >
          {create.isPending ? "Creating…" : "Create dataset"}
        </button>
        <button
          type="button"
          onClick={onCancelled}
          className="cursor-pointer rounded border border-neutral-700 px-3 py-1.5 text-sm text-neutral-300 hover:bg-neutral-800"
        >
          Cancel
        </button>
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
        <p className="text-sm text-red-400">{error.message}</p>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-4xl px-6 py-8">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">Evaluations</h1>
        <button
          type="button"
          onClick={() => setCreating(true)}
          className="cursor-pointer rounded bg-neutral-100 px-3 py-1.5 text-sm font-medium text-neutral-900 hover:bg-white"
        >
          New dataset
        </button>
      </div>
      <p className="mt-1 text-sm text-neutral-400">
        Test-case datasets scored against your agents' published versions.
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
        <p className="mt-10 text-sm text-neutral-400">
          No datasets yet. Create one to get started.
        </p>
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
                  <button
                    type="button"
                    onClick={() => {
                      if (!window.confirm(`Delete dataset "${dataset.name}"?`)) return;
                      deleteDataset.mutate(dataset.id, {
                        onError: (err) => toast("error", err.message),
                      });
                    }}
                    className="cursor-pointer text-xs text-red-400 hover:text-red-300"
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