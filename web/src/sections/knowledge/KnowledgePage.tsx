import { useState } from "react";
import type { FormEvent } from "react";
import { Link } from "react-router";

import {
  useCreateKnowledgeDataset,
  useDeleteKnowledgeDataset,
  useKnowledgeDatasets,
  type KnowledgeDataset,
} from "@/api/queries/knowledge";
import { SectionGate } from "@/capabilities/SectionGate";
import { toast } from "@/stores/toast";

// Knowledge (S8, ADR 0020 §6, D63): the dataset list. The embedding config
// is pinned AT CREATE — the dimension is load-bearing for every stored
// vector, so the form is create-only and the detail page shows it
// read-only. A delete that an agent version snapshot still references is a
// 409 the toast relays verbatim (the immutable-snapshot pin).

const inputClass =
  "w-full rounded border border-neutral-700 bg-neutral-950 px-2 py-1 text-sm text-neutral-100";

function DatasetRow({ dataset }: { dataset: KnowledgeDataset }) {
  const remove = useDeleteKnowledgeDataset();
  const [removing, setRemoving] = useState(false);

  const onDelete = () => {
    setRemoving(true);
    remove.mutate(dataset.id, {
      onError: (err: Error) => {
        toast("error", err.message);
        setRemoving(false);
      },
      onSuccess: () => setRemoving(false),
    });
  };

  return (
    <li className="rounded border border-neutral-800 bg-neutral-900 p-4" data-testid="dataset-row">
      <div className="flex items-center justify-between gap-3">
        <div className="min-w-0">
          <h3 className="text-sm font-medium text-neutral-100">
            <Link to={`/knowledge/${dataset.id}`} className="hover:underline">
              {dataset.name}
            </Link>
          </h3>
          <p className="mt-1 flex flex-wrap items-center gap-2 text-xs text-neutral-400">
            <span className="font-mono">
              {dataset.embedding_config.provider}/{dataset.embedding_config.model}
            </span>
            <span className="rounded bg-neutral-800 px-1.5 py-0.5 font-mono">
              dim {dataset.embedding_config.dimension}
            </span>
            {dataset.description !== "" && (
              <span className="truncate text-neutral-500">{dataset.description}</span>
            )}
          </p>
        </div>
        <button
          type="button"
          onClick={onDelete}
          disabled={removing}
          className="cursor-pointer rounded border border-neutral-700 px-2 py-1 text-xs text-neutral-300 hover:bg-neutral-800 disabled:opacity-50"
        >
          {removing ? "Deleting…" : "Delete"}
        </button>
      </div>
    </li>
  );
}

function CreateDatasetForm() {
  const create = useCreateKnowledgeDataset();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [provider, setProvider] = useState("mock");
  const [model, setModel] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [dimension, setDimension] = useState(8);
  const [error, setError] = useState<string | null>(null);

  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    setError(null);
    create.mutate(
      {
        name: name.trim(),
        description,
        embedding_config: {
          provider,
          model: model.trim(),
          ...(baseUrl.trim() !== "" ? { base_url: baseUrl.trim() } : {}),
          dimension,
          distance: "cosine" as const,
        },
        chunk_config: { chunk_chars: 1200, overlap_chars: 150 },
      },
      {
        onSuccess: () => {
          setName("");
          setDescription("");
          setModel("");
          setBaseUrl("");
        },
        onError: (err: Error) => setError(err.message),
      },
    );
  };

  return (
    <form
      onSubmit={onSubmit}
      className="rounded border border-neutral-800 bg-neutral-900 p-4"
      data-testid="create-dataset-form"
    >
      <h2 className="text-sm font-medium text-neutral-100">New dataset</h2>
      <p className="mt-1 text-xs text-neutral-500">
        The embedding config is pinned at create — the dimension is load-bearing
        for every stored vector and cannot change afterwards.
      </p>
      <div className="mt-3 grid gap-3 sm:grid-cols-2">
        <label className="block">
          <span className="text-xs font-medium text-neutral-400">Name</span>
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            className={inputClass}
            data-testid="dataset-name-input"
          />
        </label>
        <label className="block">
          <span className="text-xs font-medium text-neutral-400">Provider</span>
          <select
            value={provider}
            onChange={(e) => setProvider(e.target.value)}
            className={inputClass}
            data-testid="embedding-provider-input"
          >
            <option value="mock">mock</option>
            <option value="openai_compatible">openai_compatible</option>
          </select>
        </label>
        <label className="block">
          <span className="text-xs font-medium text-neutral-400">Embedding model</span>
          <input
            value={model}
            onChange={(e) => setModel(e.target.value)}
            placeholder="qwen3-embedding:0.6b"
            className={inputClass}
            data-testid="embedding-model-input"
          />
        </label>
        <label className="block">
          <span className="text-xs font-medium text-neutral-400">Base URL (optional)</span>
          <input
            value={baseUrl}
            onChange={(e) => setBaseUrl(e.target.value)}
            placeholder="http://127.0.0.1:11434/v1"
            className={inputClass}
            data-testid="embedding-base-url-input"
          />
        </label>
        <label className="block">
          <span className="text-xs font-medium text-neutral-400">Dimension</span>
          <input
            type="number"
            min={1}
            value={dimension}
            onChange={(e) => setDimension(Number(e.target.value))}
            className={inputClass}
            data-testid="embedding-dimension-input"
          />
        </label>
        <label className="block sm:col-span-2">
          <span className="text-xs font-medium text-neutral-400">Description</span>
          <input
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            className={inputClass}
          />
        </label>
      </div>
      {error && (
        <p role="alert" className="mt-2 text-xs text-red-400">
          {error}
        </p>
      )}
      <button
        type="submit"
        disabled={create.isPending || name.trim() === "" || model.trim() === ""}
        className="mt-3 cursor-pointer rounded border border-neutral-600 bg-neutral-800 px-3 py-1 text-xs text-neutral-100 hover:bg-neutral-700 disabled:cursor-not-allowed disabled:opacity-50"
      >
        {create.isPending ? "Creating…" : "Create dataset"}
      </button>
    </form>
  );
}

export default function KnowledgePage() {
  const datasets = useKnowledgeDatasets();
  return (
    <SectionGate sectionKey="knowledge">
      <div className="mx-auto max-w-3xl px-6 py-8">
        <h1 className="text-2xl font-semibold text-neutral-100">Knowledge</h1>
        <p className="mt-1 text-sm text-neutral-400">
          Datasets of embedded text chunks an agent retrieves from — auto-injected
          before each turn or called as a <code className="font-mono">retriever__</code> tool.
        </p>
        {datasets.isPending && (
          <p className="mt-6 text-sm text-neutral-400">Loading datasets…</p>
        )}
        {datasets.isError && (
          <p role="alert" className="mt-6 text-sm text-red-400">
            {datasets.error.message}
          </p>
        )}
        {datasets.data && (
          <>
            {datasets.data.length === 0 ? (
              <p className="mt-6 text-sm text-neutral-500" data-testid="no-datasets">
                No datasets yet — create one below.
              </p>
            ) : (
              <ul className="mt-6 space-y-3" data-testid="dataset-list">
                {datasets.data.map((dataset) => (
                  <DatasetRow key={dataset.id} dataset={dataset} />
                ))}
              </ul>
            )}
            <div className="mt-6">
              <CreateDatasetForm />
            </div>
          </>
        )}
      </div>
    </SectionGate>
  );
}
