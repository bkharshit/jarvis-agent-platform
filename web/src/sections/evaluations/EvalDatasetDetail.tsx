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
  type ScorerConfig,
} from "@/api/queries/evaluations";
import { toast } from "@/stores/toast";

// Dataset detail (S11, ADR 0017 §6): the mutable fields PATCH wholesale
// (the PATCH discipline) — cases/scorers/judge_model as one payload. The
// backend owns validation: llm_judge without a judge_model is a 422 the
// banner relays verbatim. Eval runs list below derives status server-side
// (D49); a run is created against the agent's LATEST PUBLISHED version —
// pinning is a backend fact, reported in the run's summary after 202.

const SCORER_NAMES = [
  "exact",
  "contains",
  "regex",
  "json_schema",
  "tool_sequence",
  "llm_judge",
] as const;

type ScorerName = (typeof SCORER_NAMES)[number];

interface Draft {
  name: string;
  description: string;
  cases: EvalCase[];
  scorers: ScorerConfig[];
  judgeProvider: string;
  judgeModel: string;
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

  const judgeModel =
    draft.judgeProvider.trim() && draft.judgeModel.trim()
      ? { provider: draft.judgeProvider.trim(), model: draft.judgeModel.trim() }
      : null;

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
      <button
        type="button"
        onClick={() => navigate("/evaluations")}
        className="text-xs text-neutral-400 hover:text-neutral-200"
      >
        ← Evaluations
      </button>
      <h1 className="mt-2 text-xl font-semibold text-neutral-100">{dataset.name}</h1>
      <p className="mt-1 font-mono text-xs text-neutral-500">{dataset.id}</p>

      {/* --- mutable fields (wholesale PATCH) --- */}
      <div className="mt-6 rounded border border-neutral-800 bg-neutral-900 p-4">
        <label className="block text-sm text-neutral-300" htmlFor="ds-name">
          Name
        </label>
        <input
          id="ds-name"
          value={draft.name}
          onChange={(event) => setDraft({ ...draft, name: event.target.value })}
          className="mt-1 w-full rounded border border-neutral-700 bg-neutral-800 px-2 py-1 text-sm text-neutral-100"
        />
        <label className="mt-3 block text-sm text-neutral-300" htmlFor="ds-description">
          Description
        </label>
        <input
          id="ds-description"
          value={draft.description}
          onChange={(event) => setDraft({ ...draft, description: event.target.value })}
          className="mt-1 w-full rounded border border-neutral-700 bg-neutral-800 px-2 py-1 text-sm text-neutral-100"
        />

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
                  <input
                    value={evalCase.id}
                    onChange={(event) =>
                      setDraft({
                        ...draft,
                        cases: draft.cases.map((aCase, at) =>
                          at === index ? { ...aCase, id: event.target.value } : aCase,
                        ),
                      })
                    }
                    className="w-24 rounded border border-neutral-700 bg-neutral-800 px-1 py-0.5 font-mono text-xs text-neutral-100"
                  />
                </td>
                <td className="py-1 pr-3">
                  <input
                    value={evalCase.input}
                    onChange={(event) =>
                      setDraft({
                        ...draft,
                        cases: draft.cases.map((aCase, at) =>
                          at === index ? { ...aCase, input: event.target.value } : aCase,
                        ),
                      })
                    }
                    className="w-full rounded border border-neutral-700 bg-neutral-800 px-2 py-0.5 text-xs text-neutral-100"
                  />
                </td>
                <td className="py-1 pr-3">
                  <input
                    value={evalCase.expected ?? ""}
                    onChange={(event) =>
                      setDraft({
                        ...draft,
                        cases: draft.cases.map((aCase, at) =>
                          at === index ? { ...aCase, expected: event.target.value } : aCase,
                        ),
                      })
                    }
                    className="w-full rounded border border-neutral-700 bg-neutral-800 px-2 py-0.5 text-xs text-neutral-100"
                  />
                </td>
                <td className="py-1">
                  <button
                    type="button"
                    onClick={() =>
                      setDraft({
                        ...draft,
                        cases: draft.cases.filter((_, at) => at !== index),
                      })
                    }
                    className="cursor-pointer text-xs text-red-400 hover:text-red-300"
                  >
                    Remove
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <button
          type="button"
          onClick={() =>
            setDraft({
              ...draft,
              cases: [...draft.cases, { id: `c-${draft.cases.length + 1}`, input: "", expected: "" }],
            })
          }
          className="mt-2 cursor-pointer rounded border border-neutral-700 px-2 py-1 text-xs text-neutral-300 hover:bg-neutral-800"
        >
          + Add case
        </button>

        <h2 className="mt-4 text-sm font-medium text-neutral-300">Scorers</h2>
        <ul className="mt-2 flex flex-col gap-2">
          {draft.scorers.map((scorer, index) => (
            <li key={index} className="rounded border border-neutral-800 p-2">
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
                  className="rounded border border-neutral-700 bg-neutral-800 px-2 py-0.5 text-xs text-neutral-100"
                >
                  {SCORER_NAMES.map((name) => (
                    <option key={name} value={name}>
                      {name}
                    </option>
                  ))}
                </select>
                <button
                  type="button"
                  onClick={() =>
                    setDraft({
                      ...draft,
                      scorers: draft.scorers.filter((_, where) => where !== index),
                    })
                  }
                  className="cursor-pointer text-xs text-red-400 hover:text-red-300"
                >
                  Remove
                </button>
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
        <button
          type="button"
          onClick={() => setDraft({ ...draft, scorers: [...draft.scorers, { name: "exact" }] })}
          className="mt-2 cursor-pointer rounded border border-neutral-700 px-2 py-1 text-xs text-neutral-300 hover:bg-neutral-800"
        >
          + Add scorer
        </button>

        <h2 className="mt-4 text-sm font-medium text-neutral-300">Judge model (for llm_judge)</h2>
        <div className="mt-2 flex gap-2">
          <input
            value={draft.judgeProvider}
            placeholder="provider (e.g. openai_compatible)"
            onChange={(event) => setDraft({ ...draft, judgeProvider: event.target.value })}
            className="w-1/2 rounded border border-neutral-700 bg-neutral-800 px-2 py-0.5 font-mono text-xs text-neutral-100"
          />
          <input
            value={draft.judgeModel}
            placeholder="model"
            onChange={(event) => setDraft({ ...draft, judgeModel: event.target.value })}
            className="w-1/2 rounded border border-neutral-700 bg-neutral-800 px-2 py-0.5 font-mono text-xs text-neutral-100"
          />
          {judgeModel !== null && (
            <button
              type="button"
              onClick={() => setDraft({ ...draft, judgeProvider: "", judgeModel: "" })}
              className="cursor-pointer text-xs text-neutral-400 hover:text-neutral-200"
            >
              Clear
            </button>
          )}
        </div>

        <button
          type="button"
          onClick={save}
          disabled={update.isPending}
          className="mt-4 cursor-pointer rounded bg-neutral-100 px-3 py-1.5 text-sm font-medium text-neutral-900 hover:bg-white disabled:opacity-50"
        >
          {update.isPending ? "Saving…" : "Save changes"}
        </button>
      </div>

      {/* --- run an evaluation --- */}
      <div className="mt-6 rounded border border-neutral-800 bg-neutral-900 p-4">
        <h2 className="text-sm font-medium text-neutral-300">Run evaluation</h2>
        <p className="mt-1 text-xs text-neutral-500">
          One ordinary run per case, pinned to the agent's latest published version.
        </p>
        <div className="mt-2 flex items-center gap-2">
          <select
            value={agentId}
            onChange={(event) => setAgentId(event.target.value)}
            className="rounded border border-neutral-700 bg-neutral-800 px-2 py-1 text-sm text-neutral-100"
          >
            <option value="">Pick an agent…</option>
            {(agents ?? []).map((agent) => (
              <option key={agent.id} value={agent.id}>
                {agent.name}
              </option>
            ))}
          </select>
          <button
            type="button"
            disabled={agentId === "" || createRun.isPending}
            onClick={() =>
              agentId !== "" &&
              createRun.mutate(agentId, {
                onSuccess: (summary) => navigate(`/evaluations/runs/${summary.id}`),
                onError: (err) => toast("error", err.message),
              })
            }
            className="cursor-pointer rounded bg-neutral-100 px-3 py-1.5 text-sm font-medium text-neutral-900 hover:bg-white disabled:opacity-50"
          >
            {createRun.isPending ? "Queuing…" : "Run"}
          </button>
        </div>
      </div>

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
                  <td className="py-1 text-neutral-300">{run.status}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <button
        type="button"
        onClick={() => {
          if (!window.confirm(`Delete dataset "${dataset.name}"?`)) return;
          remove.mutate(dataset.id, {
            onSuccess: () => navigate("/evaluations"),
            onError: (err) => toast("error", err.message),
          });
        }}
        className="mt-8 cursor-pointer text-xs text-red-400 hover:text-red-300"
      >
        Delete dataset
      </button>
    </div>
  );
}