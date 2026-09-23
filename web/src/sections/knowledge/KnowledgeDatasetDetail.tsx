import { useState } from "react";
import type { ChangeEvent, FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router";

import {
  useCreateKnowledgeDocument,
  useDeleteKnowledgeDocument,
  useKnowledgeDataset,
  useKnowledgeDocuments,
  type KnowledgeDocument,
} from "@/api/queries/knowledge";
import { SectionGate } from "@/capabilities/SectionGate";
import { toast } from "@/stores/toast";

// Dataset detail (S8, ADR 0020 §6, D65): the document table with an ingest
// form. Ingest is SYNCHRONOUS — the create request blocks on chunk → embed
// → persist; a failed embed still persists an honest `error` row (the API
// answers 502 and the toast relays it), so the table always shows reality.
// Text comes from the paste box or a .txt/.md file read client-side with
// FileReader — no server upload path in this build.

const statusChip: Record<KnowledgeDocument["status"], string> = {
  ready: "bg-green-950 text-green-300",
  error: "bg-red-950 text-red-300",
};

function DocumentRow({ document }: { document: KnowledgeDocument }) {
  const { datasetId } = useParams();
  const remove = useDeleteKnowledgeDocument(datasetId ?? "");
  const [removing, setRemoving] = useState(false);

  const onDelete = () => {
    setRemoving(true);
    remove.mutate(document.id, {
      onError: (err: Error) => {
        toast("error", err.message);
        setRemoving(false);
      },
      onSuccess: () => setRemoving(false),
    });
  };

  return (
    <li className="rounded border border-neutral-800 bg-neutral-900 p-3" data-testid="document-row">
      <div className="flex items-center justify-between gap-3">
        <div className="min-w-0">
          <h3 className="text-sm font-medium text-neutral-100">{document.name}</h3>
          <p className="mt-1 flex items-center gap-2 text-xs text-neutral-400">
            <span
              className={`rounded px-1.5 py-0.5 font-mono ${statusChip[document.status]}`}
              data-testid="document-status"
            >
              {document.status}
            </span>
            <span className="font-mono">{document.chunk_count} chunks</span>
          </p>
          {document.status === "error" && document.error !== null && (
            <p className="mt-1 break-words text-xs text-red-400" data-testid="document-error">
              {document.error}
            </p>
          )}
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

function IngestForm() {
  const { datasetId } = useParams();
  const create = useCreateKnowledgeDocument(datasetId ?? "");
  const [name, setName] = useState("");
  const [content, setContent] = useState("");
  const [error, setError] = useState<string | null>(null);

  const onFile = (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    if (file === undefined) return;
    // Client-side read: the file NEVER leaves the browser until the user
    // submits the (editable) textarea — same discipline as pasted text.
    const reader = new FileReader();
    reader.onload = () => {
      const text = typeof reader.result === "string" ? reader.result : "";
      if (name.trim() === "") setName(file.name.replace(/\.(txt|md|markdown)$/i, ""));
      setContent(text);
    };
    void reader.readAsText(file);
  };

  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    setError(null);
    create.mutate(
      { name: name.trim(), content },
      {
        onSuccess: () => {
          setName("");
          setContent("");
        },
        onError: (err: Error) => setError(err.message),
      },
    );
  };

  return (
    <form
      onSubmit={onSubmit}
      className="rounded border border-neutral-800 bg-neutral-900 p-4"
      data-testid="ingest-form"
    >
      <h2 className="text-sm font-medium text-neutral-100">Add document</h2>
      <p className="mt-1 text-xs text-neutral-500">
        Ingest chunks and embeds the text NOW (synchronous) — the button
        returns with the document ready or an honest error row.
      </p>
      <div className="mt-3 grid gap-3">
        <label className="block">
          <span className="text-xs font-medium text-neutral-400">Name</span>
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            className="w-full rounded border border-neutral-700 bg-neutral-950 px-2 py-1 text-sm text-neutral-100"
            data-testid="document-name-input"
          />
        </label>
        <label className="block">
          <span className="text-xs font-medium text-neutral-400">Content</span>
          <textarea
            value={content}
            onChange={(e) => setContent(e.target.value)}
            rows={6}
            className="w-full rounded border border-neutral-700 bg-neutral-950 px-2 py-1 font-mono text-sm text-neutral-100"
            data-testid="document-content-input"
          />
        </label>
        <label className="block text-xs text-neutral-400">
          …or read a .txt / .md file (its text lands in the box above, editable):
          <input
            type="file"
            accept=".txt,.md,.markdown,text/plain,text/markdown"
            onChange={onFile}
            className="mt-1 block text-xs"
            data-testid="document-file-input"
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
        disabled={create.isPending || name.trim() === "" || content.trim() === ""}
        className="mt-3 cursor-pointer rounded border border-neutral-600 bg-neutral-800 px-3 py-1 text-xs text-neutral-100 hover:bg-neutral-700 disabled:cursor-not-allowed disabled:opacity-50"
      >
        {create.isPending ? "Ingesting…" : "Ingest document"}
      </button>
    </form>
  );
}

export default function KnowledgeDatasetDetail() {
  const { datasetId = "" } = useParams();
  const navigate = useNavigate();
  const dataset = useKnowledgeDataset(datasetId);
  const documents = useKnowledgeDocuments(datasetId);

  if (dataset.isPending) {
    return <p className="px-6 py-10 text-sm text-neutral-400">Loading dataset…</p>;
  }
  if (dataset.isError) {
    return (
      <div className="px-6 py-10">
        <p role="alert" className="text-sm text-red-400">
          {dataset.error.message}
        </p>
        <button
          type="button"
          onClick={() => void navigate("/knowledge")}
          className="mt-4 cursor-pointer text-sm text-neutral-300 underline"
        >
          Back to knowledge
        </button>
      </div>
    );
  }
  const body = dataset.data!;

  return (
    <SectionGate sectionKey="knowledge">
      <div className="mx-auto max-w-3xl px-6 py-8">
        <p className="text-xs text-neutral-500">
          <Link to="/knowledge" className="hover:underline">
            Knowledge
          </Link>
          {" / "}
          <span className="text-neutral-400">{body.name}</span>
        </p>
        <h1 className="mt-1 text-2xl font-semibold text-neutral-100">{body.name}</h1>
        <p className="mt-2 text-xs text-neutral-400" data-testid="embedding-config">
          <span className="font-mono">{body.embedding_config.provider}</span>
          {" / "}
          <span className="font-mono">{body.embedding_config.model}</span>
          {" — "}
          <span className="font-mono">dim {body.embedding_config.dimension}</span>
          {" — "}
          <span className="font-mono">{body.embedding_config.distance}</span>
          <span className="text-neutral-500"> (pinned at create — immutable)</span>
        </p>
        {body.description !== "" && (
          <p className="mt-1 text-sm text-neutral-400">{body.description}</p>
        )}

        <h2 className="mt-8 text-sm font-medium text-neutral-100">Documents</h2>
        {documents.isPending && (
          <p className="mt-3 text-sm text-neutral-400">Loading documents…</p>
        )}
        {documents.data && documents.data.length === 0 && (
          <p className="mt-3 text-sm text-neutral-500" data-testid="no-documents">
            No documents yet — ingest one below.
          </p>
        )}
        {documents.data && documents.data.length > 0 && (
          <ul className="mt-3 space-y-2" data-testid="document-list">
            {documents.data.map((doc) => (
              <DocumentRow key={doc.id} document={doc} />
            ))}
          </ul>
        )}
        <div className="mt-6">
          <IngestForm />
        </div>
      </div>
    </SectionGate>
  );
}
