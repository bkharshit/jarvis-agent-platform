import { useState } from "react";
import type { FormEvent } from "react";
import { Link } from "react-router";
import { BookOpen, Plus } from "lucide-react";

import {
  useCreateKnowledgeDataset,
  useDeleteKnowledgeDataset,
  useKnowledgeDatasets,
  type KnowledgeDataset,
} from "@/api/queries/knowledge";
import { PageHeader } from "@/components/catalog/PageHeader";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";
import { Field } from "@/components/ui/Field";
import { Input } from "@/components/ui/Input";
import { SectionGate } from "@/capabilities/SectionGate";
import { toast } from "@/stores/toast";

// Knowledge (S8, ADR 0020 §6, D63): the dataset list. The embedding config
// is pinned AT CREATE — the dimension is load-bearing for every stored
// vector, so the form is create-only and the detail page shows it
// read-only. A delete that an agent version snapshot still references is a
// 409 the toast relays verbatim (the immutable-snapshot pin).

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
    <li
      className="flex items-center justify-between gap-3 rounded-xl border border-neutral-800/80 bg-neutral-900/50 p-4 shadow-sm transition-all hover:border-neutral-700/80"
      data-testid="dataset-row"
    >
      <div className="min-w-0">
        <h3 className="text-sm font-medium text-neutral-100">
          <Link to={`/knowledge/${dataset.id}`} className="hover:underline">
            {dataset.name}
          </Link>
        </h3>
        <p className="mt-1 flex flex-wrap items-center gap-2 text-xs text-neutral-400">
          <Badge variant="neutral" className="font-mono">
            {dataset.embedding_config.provider}/{dataset.embedding_config.model}
          </Badge>
          <Badge variant="brand" className="font-mono">
            dim {dataset.embedding_config.dimension}
          </Badge>
          {dataset.description !== "" && (
            <span className="truncate text-neutral-500">{dataset.description}</span>
          )}
        </p>
      </div>
      <Button
        variant="ghost"
        size="sm"
        onClick={onDelete}
        disabled={removing}
        className="text-red-400 hover:bg-rose-950/40 hover:text-red-300"
      >
        {removing ? "Deleting…" : "Delete"}
      </Button>
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
    <form onSubmit={onSubmit} data-testid="create-dataset-form">
      <Card className="p-4">
        <h2 className="text-sm font-medium text-neutral-100">New dataset</h2>
        <p className="mt-1 text-xs text-neutral-500">
          The embedding config is pinned at create — the dimension is load-bearing
          for every stored vector and cannot change afterwards.
        </p>
        <div className="mt-3 grid gap-3 sm:grid-cols-2">
          <Field label="Name">
            <Input
              value={name}
              onChange={(e) => setName(e.target.value)}
              data-testid="dataset-name-input"
            />
          </Field>
          <Field label="Provider">
            <select
              value={provider}
              onChange={(e) => setProvider(e.target.value)}
              className="w-full cursor-pointer rounded-md border border-neutral-800 bg-neutral-900/70 px-3 py-1.5 text-sm text-neutral-100 transition-colors focus:border-indigo-500 focus:outline-none focus:ring-2 focus:ring-indigo-500/30"
              data-testid="embedding-provider-input"
            >
              <option value="mock">mock</option>
              <option value="openai_compatible">openai_compatible</option>
            </select>
          </Field>
          <Field label="Embedding model">
            <Input
              value={model}
              onChange={(e) => setModel(e.target.value)}
              placeholder="qwen3-embedding:0.6b"
              data-testid="embedding-model-input"
            />
          </Field>
          <Field label="Base URL (optional)">
            <Input
              value={baseUrl}
              onChange={(e) => setBaseUrl(e.target.value)}
              placeholder="http://127.0.0.1:11434/v1"
              data-testid="embedding-base-url-input"
            />
          </Field>
          <Field label="Dimension">
            <Input
              type="number"
              min={1}
              value={dimension}
              onChange={(e) => setDimension(Number(e.target.value))}
              data-testid="embedding-dimension-input"
            />
          </Field>
          <Field label="Description" className="sm:col-span-1">
            <Input value={description} onChange={(e) => setDescription(e.target.value)} />
          </Field>
        </div>
        {error && (
          <p role="alert" className="mt-2 text-xs text-red-400">
            {error}
          </p>
        )}
        <Button
          type="submit"
          variant="secondary"
          size="sm"
          className="mt-3"
          icon={<Plus aria-hidden />}
          disabled={create.isPending || name.trim() === "" || model.trim() === ""}
        >
          {create.isPending ? "Creating…" : "Create dataset"}
        </Button>
      </Card>
    </form>
  );
}

export default function KnowledgePage() {
  const datasets = useKnowledgeDatasets();
  const [search, setSearch] = useState("");
  const [view, setView] = useState<"grid" | "table">("grid");

  const needle = search.trim().toLowerCase();
  const visible = (datasets.data ?? []).filter(
    (dataset) =>
      needle === "" ||
      dataset.name.toLowerCase().includes(needle) ||
      dataset.description.toLowerCase().includes(needle),
  );

  return (
    <SectionGate sectionKey="knowledge">
      <div>
        <PageHeader
          title="Knowledge"
          description="Datasets of embedded text chunks an agent retrieves from — auto-injected before each turn or called as a retriever__ tool."
          search={{
            value: search,
            onChange: setSearch,
            placeholder: "Search datasets…",
          }}
          view={{ value: view, onChange: setView }}
        />
        {datasets.isPending && (
          <p className="px-6 py-10 text-sm text-neutral-400">Loading datasets…</p>
        )}
        {datasets.isError && (
          <p role="alert" className="px-6 py-10 text-sm text-red-400">
            {datasets.error.message}
          </p>
        )}
        {datasets.data && (
          <div className="px-6 py-6">
            {datasets.data.length === 0 ? (
              <div data-testid="no-datasets">
                <EmptyState
                  icon={<BookOpen className="h-6 w-6" />}
                  title="No datasets yet"
                  description="Create one below — paste or upload text, and the chunker + embedder do the rest."
                />
              </div>
            ) : visible.length === 0 ? (
              <EmptyState
                icon={<BookOpen className="h-6 w-6" />}
                title="No datasets match"
                description="Nothing matches this search — try a different term."
              />
            ) : (
              <ul
                className={
                  view === "grid"
                    ? "grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3"
                    : "flex flex-col gap-3"
                }
                data-testid="dataset-list"
              >
                {visible.map((dataset) => (
                  <DatasetRow key={dataset.id} dataset={dataset} />
                ))}
              </ul>
            )}
            <div className="mt-6">
              <CreateDatasetForm />
            </div>
          </div>
        )}
      </div>
    </SectionGate>
  );
}