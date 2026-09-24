import { Link, useParams } from "react-router";

import { useEvalRun, type Score } from "@/api/queries/evaluations";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";

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
        const variant =
          score.passed === true
            ? "success"
            : score.passed === false
              ? "danger"
              : "neutral";
        return (
          <Badge key={score.scorer} variant={variant} title={score.detail ?? undefined} className="font-mono">
            {score.scorer}: {score.passed === null ? "no verdict" : score.passed ? "pass" : "fail"}
            {score.score != null ? ` (${score.score})` : ""}
          </Badge>
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
      <Button
        variant="ghost"
        size="sm"
        className="self-start px-0 text-xs hover:bg-transparent"
        onClick={() => window.history.back()}
      >
        ← Back
      </Button>
      <h1 className="mt-2 flex items-center gap-3 text-xl font-semibold text-neutral-100">
        Eval run
        <Badge variant="neutral">{run.status}</Badge>
      </h1>
      <Card className="mt-3 p-4 text-sm">
        <dl>
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
      </Card>

      <h2 className="mt-6 text-sm font-medium text-neutral-300">Per-case results</h2>
      {run.results.length === 0 ? (
        <p className="mt-2 text-sm text-neutral-400">No cases in the snapshot.</p>
      ) : (
        <ul className="mt-2 flex flex-col gap-2">
          {run.results.map((result) => (
            <li
              key={result.id}
              className="rounded-xl border border-neutral-800/80 bg-neutral-900/50 p-4 shadow-sm transition-all hover:border-neutral-700/80"
            >
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