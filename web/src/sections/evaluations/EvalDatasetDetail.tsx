import { useNavigate, useParams } from "react-router";
import { useEffect, useState } from "react";

import { useAgents } from "@/api/queries/agents";
import {
  useCreateEvalRun,
  useDeleteEvalDataset,
  useEvalDataset,
  useEvalRuns,
  useUpdateEvalDataset,
  type EvalCase,
  type ModelRef,
  type ScorerConfig,
} from "@/api/queries/evaluations";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { Field } from "@/components/ui/Field";
import { Input } from "@/components/ui/Input";
import { toast } from "@/stores/toast";

// Dataset detail (S11, ADR 0017 §6): the mutable fields PATCH wholesale
// (the PATCH discipline) — cases/scorers/judge_model as one payload. The
// backend owns validation: llm_judge without a judge_model is a 422 the
// banner relays verbatim. Eval runs list below derives status server-side
// (D49); a run is created against the agent's LATEST PUBLISHED version —
// pinning is a backend fact, reported in the run's summary after 202.
//
// The judge panel collects only provider + model; a judge_model can carry
// more (base_url, credential_ref — curl-only creation for now). The
// original ref is kept on the draft and rebuilt on save, so the wholesale
// PATCH never silently drops the fields this panel doesn't collect (found
// live in the S11 joint session); they render read-only below the inputs.

const SCORER_NAMES = [
  "exact",
  "contains",
  "regex",
  "json_schema",
  "tool_sequence",
  "llm_judge",
] as const;

type ScorerName = (typeof SCORER_NAMES)[number];

const selectClass =
  "cursor-pointer rounded-md border border-neutral-800 bg-neutral-900/70 px-3 py-1.5 text-sm text-neutral-100 transition-colors focus:border-indigo-500 focus:outline-none focus:ring-2 focus:ring-indigo-500/30";

interface Draft {
  name: string;
  description: string;
  cases: EvalCase[];
  scorers: ScorerConfig[];
  judgeProvider: string;
  judgeModel: string;
  /** the judge_model as the API returned it — the save rebuilds from this
   * so fields the panel doesn't collect (base_url, credential_ref) survive */
  judgeModelRef: ModelRef | null;
}

function ScorerParamsHint({ scorerName }: { scorerName: string }) {
  const hints: Record<string, string> = {
    regex: 'params.pattern — matched against the final answer (DOTALL)',
    json_schema: "params.schema — the final answer must parse as JSON and validate",
    tool_sequence: "params.expected — the exact tool-name order to observe",
    exact: "no params — the trimmed final answer equals expected",
    contains: "no params — expected is a substring of the final answer",
    llm_judge: "requires the dataset judge_model below (D50)",
  };
  return <p className="mt-1 text-xs text-neutral-500">{hints[scorerName] ?? ""}</p>;
}

export function EvalDatasetDetail() {
  const { datasetId } = useParams<{ datasetId: string }>();
  const navigate = useNavigate();
  const { data: dataset, isPending, isError, error } = useEvalDataset(datasetId);
  const { data: agents } = useAgents();
  const { data: runs } = useEvalRuns({ datasetId });
  const update = useUpdateEvalDataset(datasetId ?? "");
  const createRun = useCreateEvalRun(datasetId ?? "");
  const remove = useDeleteEvalDataset();

  const [draft, setDraft] = useState<Draft | null>(null);
  const [agentId, setAgentId] = useState("");

  useEffect(() => {
    if (dataset && draft === null) {
      setDraft({
        name: dataset.name,
        description: dataset.description,
        cases: dataset.cases.map((aCase) => ({ ...aCase })),
        scorers: dataset.scorers.map((scorer) => ({ ...scorer, params: { ...scorer.params } })),
        judgeProvider: dataset.judge_model?.provider ?? "",
        judgeModel: dataset.judge_model?.model ?? "",
        judgeModelRef: dataset.judge_model ?? null,
      });
    }
  }, [dataset, draft]);

  if (isPending) {
    return <p className="px-6 py-10 text-sm text-neutral-400">Loading dataset…</p>;
  }
  if (isError) {
    return (
      <div className="px-6 py-10">
        <p className="text-sm text-red-400">{error.message}</p>
      </div>
    );
  }
  if (draft === null) {
    return <p className="px-6 py-10 text-sm text-neutral-400">Loading dataset…</p>;
  }

  // rebuild from the original ref so uncollected fields (base_url,
  // credential_ref) ride the PATCH — only provider/model are editable here
  const judgeModel =
    draft.judgeProvider.trim() && draft.judgeModel.trim()
      ? {
          ...draft.judgeModelRef,
          provider: draft.judgeProvider.trim(),
          model: draft.judgeModel.trim(),
        }
      : null;
  const judgePreserved = Object.entries(draft.judgeModelRef ?? {}).filter(
    ([key]) => key !== "provider" && key !== "model",
  );

  const save = () => {
    update.mutate(
      {
        name: draft.name,
        description: draft.description,
        cases: draft.cases,
        scorers: draft.scorers,
        judge_model: judgeModel,
      },
      {
        onSuccess: () => toast("success", "Dataset saved"),
        onError: (err) => toast("error", err.message),
      },
    );
  };

  return (
    <div className="mx-auto max-w-4xl px-6 py-8">
      <Button
        variant="ghost"
        size="sm"
        className="self-start px-0 text-xs hover:bg-transparent"
        onClick={() => navigate("/evaluations")}
      >
        ← Evaluations
      </Button>
      <h1 className="mt-2 text-xl font-semibold text-neutral-100">{dataset.name}</h1>
      <p className="mt-1 font-mono text-xs text-neutral-500">{dataset.id}</p>

      {/* --- mutable fields (wholesale PATCH) --- */}
      <Card className="mt-6 p-4">
        <Field label="Name" htmlFor="ds-name">
          <Input
            id="ds-name"
            value={draft.name}
            onChange={(event) => setDraft({ ...draft, name: event.target.value })}
          />
        </Field>
        <Field label="Description" htmlFor="ds-description" className="mt-3">
          <Input
            id="ds-description"
            value={draft.description}
            onChange={(event) => setDraft({ ...draft, description: event.target.value })}
          />
        </Field>

        <h2 className="mt-4 text-sm font-medium text-neutral-300">Cases</h2>
        <table className="mt-2 w-full text-left text-sm">
          <thead className="text-neutral-400">
            <tr className="border-b border-neutral-800">
              <th className="py-1 pr-3 font-medium">Case id</th>
              <th className="py-1 pr-3 font-medium">Input</th>
              <th className="py-1 pr-3 font-medium">Expected</th>
              <th className="py-1 font-medium" />
            </tr>
          </thead>
          <tbody>
            {draft.cases.map((evalCase, index) => (
              <tr key={index}>
                <td className="py-1 pr-3">
                  <Input
                    value={evalCase.id}
                    onChange={(event) =>
                      setDraft({
                        ...draft,
                        cases: draft.cases.map((aCase, at) =>
                          at === index ? { ...aCase, id: event.target.value } : aCase,
                        ),
                      })
                    }
                    className="w-24 px-1 py-0.5 font-mono text-xs"
                  />
                </td>
                <td className="py-1 pr-3">
                  <Input
                    value={evalCase.input}
                    onChange={(event) =>
                      setDraft({
                        ...draft,
                        cases: draft.cases.map((aCase, at) =>
                          at === index ? { ...aCase, input: event.target.value } : aCase,
                        ),
                      })
                    }
                    className="px-1 py-0.5 text-xs"
                  />
                </td>
                <td className="py-1 pr-3">
                  <Input
                    value={evalCase.expected ?? ""}
                    onChange={(event) =>
                      setDraft({
                        ...draft,
                        cases: draft.cases.map((aCase, at) =>
                          at === index ? { ...aCase, expected: event.target.value } : aCase,
                        ),
                      })
                    }
                    className="px-1 py-0.5 text-xs"
                  />
                </td>
                <td className="py-1">
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() =>
                      setDraft({
                        ...draft,
                        cases: draft.cases.filter((_, at) => at !== index),
                      })
                    }
                    className="text-xs text-red-400 hover:bg-rose-950/40 hover:text-red-300"
                  >
                    Remove
                  </Button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <Button
          variant="secondary"
          size="sm"
          className="mt-2"
          onClick={() =>
            setDraft({
              ...draft,
              cases: [...draft.cases, { id: `c-${draft.cases.length + 1}`, input: "", expected: "" }],
            })
          }
        >
          + Add case
        </Button>

        <h2 className="mt-4 text-sm font-medium text-neutral-300">Scorers</h2>
        <ul className="mt-2 flex flex-col gap-2">
          {draft.scorers.map((scorer, index) => (
            <li key={index} className="rounded-lg border border-neutral-800/80 p-2">
              <div className="flex items-center gap-2">
                <select
                  value={scorer.name}
                  onChange={(event) =>
                    setDraft({
                      ...draft,
                      scorers: draft.scorers.map((at, where) =>
                        where === index
                          ? { ...at, name: event.target.value as ScorerName }
                          : at,
                      ),
                    })
                  }
                  className="cursor-pointer rounded-md border border-neutral-800 bg-neutral-900/70 px-2 py-1 text-xs text-neutral-100 focus:border-indigo-500 focus:outline-none"
                >
                  {SCORER_NAMES.map((name) => (
                    <option key={name} value={name}>
                      {name}
                    </option>
                  ))}
                </select>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() =>
                    setDraft({
                      ...draft,
                      scorers: draft.scorers.filter((_, where) => where !== index),
                    })
                  }
                  className="text-xs text-red-400 hover:bg-rose-950/40 hover:text-red-300"
                >
                  Remove
                </Button>
              </div>
              <ScorerParamsHint scorerName={scorer.name} />
              {scorer.name === "llm_judge" && judgeModel === null && (
                <p className="mt-1 text-xs text-amber-400">
                  llm_judge requires a judge model below (the backend 422s without one).
                </p>
              )}
            </li>
          ))}
        </ul>
        <Button
          variant="secondary"
          size="sm"
          className="mt-2"
          onClick={() => setDraft({ ...draft, scorers: [...draft.scorers, { name: "exact" }] })}
        >
          + Add scorer
        </Button>

        <h2 className="mt-4 text-sm font-medium text-neutral-300">Judge model (for llm_judge)</h2>
        <div className="mt-2 flex gap-2">
          <Input
            value={draft.judgeProvider}
            placeholder="provider (e.g. openai_compatible)"
            onChange={(event) => setDraft({ ...draft, judgeProvider: event.target.value })}
            className="w-1/2 px-2 py-1 font-mono text-xs"
          />
          <Input
            value={draft.judgeModel}
            placeholder="model"
            onChange={(event) => setDraft({ ...draft, judgeModel: event.target.value })}
            className="w-1/2 px-2 py-1 font-mono text-xs"
          />
          {judgeModel !== null && (
            <Button
              variant="ghost"
              size="sm"
              onClick={() => setDraft({ ...draft, judgeProvider: "", judgeModel: "" })}
              className="text-xs"
            >
              Clear
            </Button>
          )}
        </div>
        {judgePreserved.length > 0 && (
          <p className="mt-1 font-mono text-xs text-neutral-500">
            {judgePreserved.map(([key, value]) => `${key}: ${JSON.stringify(value)}`).join(" · ")}
          </p>
        )}

        <Button onClick={save} disabled={update.isPending} className="mt-4">
          {update.isPending ? "Saving…" : "Save changes"}
        </Button>
      </Card>

      {/* --- run an evaluation --- */}
      <Card className="mt-6 p-4">
        <h2 className="text-sm font-medium text-neutral-300">Run evaluation</h2>
        <p className="mt-1 text-xs text-neutral-500">
          One ordinary run per case, pinned to the agent&apos;s latest published version.
        </p>
        <div className="mt-2 flex items-center gap-2">
          <select
            value={agentId}
            onChange={(event) => setAgentId(event.target.value)}
            className={selectClass}
          >
            <option value="">Pick an agent…</option>
            {(agents ?? []).map((agent) => (
              <option key={agent.id} value={agent.id}>
                {agent.name}
              </option>
            ))}
          </select>
          <Button
            disabled={agentId === "" || createRun.isPending}
            onClick={() =>
              agentId !== "" &&
              createRun.mutate(agentId, {
                onSuccess: (summary) => navigate(`/evaluations/runs/${summary.id}`),
                onError: (err) => toast("error", err.message),
              })
            }
          >
            {createRun.isPending ? "Queuing…" : "Run"}
          </Button>
        </div>
      </Card>

      {/* --- this dataset's runs --- */}
      <div className="mt-6">
        <h2 className="text-sm font-medium text-neutral-300">Eval runs</h2>
        {(runs ?? []).length === 0 ? (
          <p className="mt-2 text-sm text-neutral-400">No runs yet.</p>
        ) : (
          <table className="mt-2 w-full text-left text-sm">
            <thead className="text-neutral-400">
              <tr className="border-b border-neutral-800">
                <th className="py-1 pr-3 font-medium">Run</th>
                <th className="py-1 pr-3 font-medium">Agent version</th>
                <th className="py-1 font-medium">Status</th>
              </tr>
            </thead>
            <tbody>
              {(runs ?? []).map((run) => (
                <tr key={run.id} className="border-b border-neutral-900">
                  <td className="py-1 pr-3">
                    <a
                      href={`/evaluations/runs/${run.id}`}
                      className="text-neutral-100 underline-offset-2 hover:underline"
                    >
                      {run.id.slice(0, 8)}…
                    </a>
                  </td>
                  <td className="py-1 pr-3 font-mono text-xs text-neutral-300">
                    {run.agent_version_id.slice(0, 8)}
                  </td>
                  <td className="py-1 text-neutral-300">
                    <Badge variant="neutral">{run.status}</Badge>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <Button
        variant="ghost"
        size="sm"
        className="mt-8 px-0 text-xs text-red-400 hover:bg-transparent hover:text-red-300"
        onClick={() => {
          if (!window.confirm(`Delete dataset "${dataset.name}"?`)) return;
          remove.mutate(dataset.id, {
            onSuccess: () => navigate("/evaluations"),
            onError: (err) => toast("error", err.message),
          });
        }}
      >
        Delete dataset
      </Button>
    </div>
  );
}