import { Link, useParams } from "react-router";

import { useEvalRun, type Score } from "@/api/queries/evaluations";

// Eval-run detail (S11, ADR 0017 §6, D49): status is a backend DERIVED
// fact (no column) and results are scored LAZILY on the first completed
// read, persisted once — the hook polls while "running", so a detail
// that arrives completed carries its scores; a later read never
// re-scores. Each result links its child run — an ordinary run in
// /executions (indistinguishable by construction).

function ScoreChips({ scores }: { scores: Score[] }) {
  return (
    <div className="flex flex-wrap gap-1">
      {scores.map((score) => {
        const passed =
          score.passed === true
            ? "bg-green-900/60 text-green-300"
            : score.passed === false
              ? "bg-red-900/60 text-red-300"
              : "bg-neutral-800 text-neutral-400";
        return (
          <span
            key={score.scorer}
            title={score.detail ?? undefined}
            className={`rounded px-2 py-0.5 font-mono text-xs ${passed}`}
          >
            {score.scorer}: {score.passed === null ? "no verdict" : score.passed ? "pass" : "fail"}
            {score.score != null ? ` (${score.score})` : ""}
          </span>
        );
      })}
    </div>
  );
}

export function EvalRunDetail() {
  const { runId } = useParams<{ runId: string }>();
  const { data: run, isPending, isError, error } = useEvalRun(runId);

  if (isPending) {
    return <p className="px-6 py-10 text-sm text-neutral-400">Loading eval run…</p>;
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
      <button
        type="button"
        onClick={() => window.history.back()}
        className="text-xs text-neutral-400 hover:text-neutral-200"
      >
        ← Back
      </button>
      <h1 className="mt-2 flex items-center gap-3 text-xl font-semibold text-neutral-100">
        Eval run
        <span className="rounded bg-neutral-800 px-2 py-0.5 text-sm text-neutral-300">{run.status}</span>
      </h1>
      <dl className="mt-3 rounded border border-neutral-800 bg-neutral-900 p-4 text-sm">
        <div className="flex gap-2">
          <dt className="w-36 shrink-0 text-neutral-400">Run id</dt>
          <dd className="font-mono text-xs text-neutral-200">{run.id}</dd>
        </div>
        <div className="mt-1 flex gap-2">
          <dt className="w-36 shrink-0 text-neutral-400">Agent / version</dt>
          <dd className="font-mono text-xs text-neutral-200">
            {run.agent_id} @ {run.agent_version_id.slice(0, 8)}
          </dd>
        </div>
        <div className="mt-1 flex gap-2">
          <dt className="w-36 shrink-0 text-neutral-400">Dataset (snapshot)</dt>
          <dd className="font-mono text-xs text-neutral-200">{run.dataset_id}</dd>
        </div>
      </dl>

      <h2 className="mt-6 text-sm font-medium text-neutral-300">Per-case results</h2>
      {run.results.length === 0 ? (
        <p className="mt-2 text-sm text-neutral-400">No cases in the snapshot.</p>
      ) : (
        <ul className="mt-2 flex flex-col gap-2">
          {run.results.map((result) => (
            <li key={result.id} className="rounded border border-neutral-800 bg-neutral-900 p-4">
              <div className="flex items-center justify-between">
                <span className="font-mono text-xs text-neutral-300">{result.case_id}</span>
                <Link
                  to={`/executions/${result.run_id}`}
                  className="text-xs text-neutral-100 underline-offset-2 hover:underline"
                >
                  child run {result.run_id.slice(0, 8)}…
                </Link>
              </div>
              {result.scores != null && result.scores.length > 0 ? (
                <div className="mt-2">
                  <ScoreChips scores={result.scores} />
                </div>
              ) : (
                <p className="mt-2 text-xs text-neutral-500">Not scored yet.</p>
              )}
              {result.error != null && (
                <p className="mt-2 text-xs text-red-400">{result.error}</p>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}