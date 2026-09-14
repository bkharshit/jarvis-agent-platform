# ADR 0017 — Evaluation framework (S11): datasets, batched runs, scores

Status: accepted (planning ADR — no S11 code exists at time of writing)
Date: 2026-09-15
Supersedes: none (implements the roadmap S11 sketch; two recorded
deviations — §2 data model, §6 route shape)

## Context

Phase 1 already persists everything an evaluation needs: runs are fully
reconstructible (events, messages, `tool_executions` rows, usage),
agent versions are immutable snapshots (D1) so an eval always executes
against a pinned version, and the queue (ADR 0008) runs ordinary runs
through workers with no new infrastructure. S11 adds the *scoring*
layer on top — no new instrumentation inside the runtime.

Verified seams this design rides on (paths checked 2026-09-15):

- `api/routes/_run_routes.py` `queue_message()` — builds a
  `RunQueueMessage` (run_id generated at enqueue time) from a
  `RunRequest` + pinned version id; `queued_result()` renders the
  `status="queued"` row. The message already carries `metadata` and
  `kind` (D41) — child eval runs need NO queue-port change.
- `persistence/repositories.py:681` `create_queued_run(result,
  message)` — row + queue message in ONE transaction; the eval runner
  reuses it per case.
- `runtime/worker.py:245` the worker resolves
  `AgentVersionLoader.get_version_by_id(message.agent_version_id)` —
  a pinned `agent_version_id` on the message is all a child run needs;
  **the worker is unchanged**.
- `api/routes/agents.py:62` the run route pins `latest_version()` at
  enqueue — the eval-run create route pins the same way (a dataset
  never rides "latest" implicitly; the eval run row records the exact
  version).
- `ports/repository.py` `ExecutionRepo.list_runs` filters agent_id /
  status / session_id / tenant only — **no metadata filter**, which
  drives the §2 decision to store child `run_id`s explicitly in
  `eval_results` rows instead of querying children by metadata.
  `list_tool_executions(run_id)` exists — scorers read the tool
  sequence from it.
- `domain/agent.py:34` `ModelRef` (provider/model/base_url/
  credential_ref) — the LLM-judge's `judge_model` is a plain ModelRef,
  resolved through the existing model factory (the D28 pattern); no
  new credential surface.
- `api/routes/models.py:41` the `ApiError(422, "validation", ...)`
  convention — dataset validation errors (missing judge model) 422 at
  create time, never at run time.
- `api/routes/capabilities.py` — `_SECTION_FLAGS` already carries
  `"evaluations": {"enabled": False, "stage": "S11", ...}`; the flip
  is a one-line change plus derived detail.
- Web: `sectionRegistry.ts` registers an `evaluations` section with
  route `/evaluations`; `app/routes.tsx:68` renders the gated
  coming-soon placeholder — the section activates behind the flag.

## Decision

### 1. Domain model (pure Pydantic, `domain/evaluation.py`)

    EvalCase:      id (stable uuid str), input, expected: str | None,
                   variables: dict[str, str] = {}
    ScorerConfig:  name: Literal["exact", "contains", "regex",
                                 "json_schema", "tool_sequence",
                                 "llm_judge"], params: dict = {}
    EvalDataset:   id, name, description, cases: list[EvalCase],
                   scorers: list[ScorerConfig],
                   judge_model: ModelRef | None = None
    EvalRun:       id, dataset_id, agent_id, agent_version_id,
                   created_at            (status derived at read, §4)
    Score:         scorer (name), passed: bool | None, score: float | None,
                   detail: str | None
    EvalResult:    id, eval_run_id, case_id, run_id, scores: list[Score]
                   | None, error: str | None, scored_at: datetime | None
    EvalObservation: status, final_message: str | None,
                   tool_names: list[str]   — what a scorer sees

`expected` is the target for text scorers; `tool_sequence`'s expected
value is its `params["expected"]` (an ordered list of tool names) — a
case's `expected` stays free-form text for judge/exact/contains/regex.

### 2. Persistence: three tables, cases as JSONB snapshots (D48)

Migration 0011 creates:

    eval_datasets  (id, tenant_id, name, description,
                    cases JSONB, scorers JSONB, judge_model JSONB NULL,
                    created_at, updated_at)
    eval_runs      (id, tenant_id, dataset_id FK, dataset JSONB,
                    agent_id, agent_version_id, created_at)
    eval_results   (id, eval_run_id FK, case_id, run_id FK executions,
                    scores JSONB NULL, error TEXT NULL, scored_at NULL;
                    UNIQUE(eval_run_id, case_id))

**Deviation from the sketch (recorded):** cases live as a JSONB list
on the dataset row — no separate `eval_cases` table. Rationale: cases
are only ever read as a whole dataset (no per-case CRUD, no case-level
queries), the D1 snapshot discipline already governs JSONB (ADR 0002),
and a stable per-case uuid inside the JSONB lets `eval_results`
reference `case_id` without an FK into a mutable table. The eval-run
row snapshots the dataset (D1 — a dataset edited later cannot rewrite
history; the run row is the exact input the scoring saw).

**Child run_ids are stored eagerly.** At eval-run create time every
case's run_id is known (§3) and written into `eval_results` — the
eval never needs a metadata-filtered child query (none exists, §
Context). Child `metadata {"eval": {"eval_run_id", "case_id"}}` is
traceability only: `/executions` shows eval runs indistinguishable
from manual runs, with the metadata visible on the row.

**Tenancy:** explicit `tenant_id` kwargs on `EvalRepo` methods (the
McpServerRepo pattern, post-S2) — D29: foreign rows read as absent.

### 3. Eval runs are ordinary runs (the worker is unchanged)

`EvalRepo` is a NEW Protocol (pre-declared per rule 7):

    class EvalRepo(Protocol):
        async def create_dataset(..., *, tenant_id) -> EvalDataset
        async def list_datasets(*, tenant_id) -> list[EvalDataset]
        async def get_dataset(dataset_id, *, tenant_id) -> EvalDataset | None
        async def update_dataset(..., *, tenant_id) -> EvalDataset | None
        async def delete_dataset(dataset_id, *, tenant_id) -> bool
        async def create_run(dataset, agent_id, agent_version_id,
                             children: list[tuple[EvalCase, str]],
                             *, tenant_id) -> EvalRun
              # children = (case, child run_id) — writes the eval_run row
              # plus one eval_result per case in ONE transaction
        async def list_runs(agent_id | None, dataset_id | None,
                            *, tenant_id) -> list[EvalRun]
        async def get_run(run_id, *, tenant_id) -> EvalRun | None
        async def get_results(eval_run_id, *, tenant_id) -> list[EvaluationResultRow]
        async def save_scores(eval_run_id, case_id, scores, error | None,
                              *, tenant_id) -> None
        async def list_version_scores(agent_id, *, tenant_id) -> ...

The eval-run create flow (route-level service, no new worker role):

1. Load the dataset (404 if absent/foreign); validate scorers —
   `llm_judge` in `scorers` requires a non-null `judge_model`, else
   **422 at create** (a dataset can never be snapshot-invalid).
2. Pin `agent_version_id = latest_version(agent_id)` exactly like the
   manual run route (404 if the agent or its published version is
   absent).
3. Per case: build a `RunQueueMessage` via the existing
   `queue_message()` (input = case.input, variables = case.variables,
   `session_id=None` — each case is an isolated run, no conversation;
   metadata stamps the eval linkage), then `create_queued_run` (one tx
   per child) and collect the run_ids.
4. `EvalRepo.create_run(...)` — eval_run + all eval_results rows in
   ONE transaction (the child run_ids all exist by now; if this final
   tx failed, the children would remain ordinary visible runs —
   acceptable, honest, and retryable).

The children enqueue through the ordinary queue; the worker picks them
up with zero changes. **An eval agent must not pause** (human-in-the-
loop): a child sitting in `awaiting_input` is non-terminal, so the
eval run stays `"running"` until the child is resolved manually (the
run is visible and resumable in `/executions` like any run) — a
recorded limitation, not a failure.

### 4. Status is derived at read; scoring is lazy, persisted once (D49)

The eval-run row carries NO status column: `GET /v1/evaluations/runs/
{id}` derives it from the child rows — any child non-terminal
(queued/running) → `"running"`; all terminal → `"completed"`. A child
that never got a run row (impossible by §3 ordering) would read as
running — honest.

Scoring runs **lazily on the detail read**, once the derived status is
`"completed"` and `scores` is still NULL: for each result, load the
child run (final message, status) + `list_tool_executions` (tool
order) into an `EvalObservation`, run the dataset's snapshot scorers,
persist via `save_scores` (`scores` + `scored_at` set). Persist-once
is the idempotency guard — a later read never re-scores; re-evaluation
is a NEW eval run (no silent score mutation). A failed child run is
scored honestly: the scorers run against the observation and the
status rides in the observation (an exact-match scorer on a failed run
fails; no special-casing).

### 5. The Scorer port and the shipped scorers (D50)

New Protocol, `ports/scoring.py`:

    class Scorer(Protocol):
        name: str
        async def score(self, case: EvalCase,
                        observation: EvalObservation) -> Score

Adapters (in-process, `scoring/`): `exact` (final message == expected,
whitespace-trimmed), `contains` (expected substring of the final
message), `regex` (`params["pattern"]`, `re.search`, DOTALL),
`json_schema` (parse the final message as JSON, validate against
`params["schema"]` — pydantic is ports-legal), `tool_sequence`
(`params["expected"]` list == the observed tool order, name-wise).
All deterministic → unit-tested without a DB or model.

`llm_judge` is the one model-backed scorer: ONE `generate()` call
through the model factory resolving the dataset's `judge_model` (the
D28 pattern — no runtime loop, no streaming, no tool surface). The
judge prompt carries the case input/expected and the observation's
final message; the response is parsed as a verdict. **Judge failure is
persisted honestly** (D5 spirit): a `ModelError` or unparseable
verdict stores `passed=null` + the error detail in the result's
`error`, never a silent retry and never a fabricated pass —
re-evaluation means a new eval run. The judge call happens OUTSIDE any
run (scoring is a read-path concern); it has no run to fail, so there
is no envelope involvement — the frozen envelope (ADR 0003) is
untouched.

### 6. API surface and UI enablement

**Route shape deviation (recorded):** top-level `/v1/evaluations*`
(the McpServerRepo pattern — a first-class resource), not the sketch's
nested `/v1/agents/{id}/evals`:

    POST   /v1/evaluations/datasets            create (422 validation)
    GET    /v1/evaluations/datasets            list
    GET    /v1/evaluations/datasets/{id}       detail (cases included)
    PATCH  /v1/evaluations/datasets/{id}       replace cases/scorers/judge
    DELETE /v1/evaluations/datasets/{id}       delete
    POST   /v1/evaluations/datasets/{id}/runs  create eval run (202)
    GET    /v1/evaluations/runs                list (filters agent_id,
                                               dataset_id)
    GET    /v1/evaluations/runs/{id}           detail: derived status +
                                               results (+ lazy scoring)
    GET    /v1/evaluations/compare?agent_id=   version comparison: scores
                                               grouped by agent_version_id

Version comparison is a query, not a feature (the sketch's own words):
`compare` aggregates eval runs + results per `agent_version_id` for
one agent — pass-rate and per-scorer means side by side.

Capabilities flip: `"evaluations"` → `enabled: true` with derived
detail (`datasets` count, `runs` count — read from the repo, the S4
derived-facts pattern). Web: the Evaluations section activates —
datasets list + editor, dataset detail, eval-run detail (per-case
scores), and the comparison view; the generated OpenAPI client is
regenerated.

### 7. What S11 does NOT touch

- The event envelope, event types, terminal semantics, `ports/queue.py`,
  the worker, the sweeper — **zero changes** (the design's core claim:
  eval runs ARE runs).
- `AgentRepo`, `ExecutionRepo` — read-only consumers (get_version, get
  run, list_tool_executions). The no-metadata-filter constraint is
  honored by storing run_ids eagerly (§2), not by adding a filter.
- Model credential rules — the judge model is a plain `ModelRef`
  resolved through the existing factory (env or stored credential,
  ADR 0006); no new secret surface.
- Agent runtime, memory (S12), workflows — an eval run of a workflow
  agent is out of scope (agent-kind children only, `kind="agent"`).

## What stays frozen

- The event envelope and every event type (ADR 0003).
- The queue contract and the worker (ADR 0008) — the eval runner is
  API-side composition of `queue_message` + `create_queued_run`.
- `RunResult` shape and the executions surface — eval children are
  indistinguishable from manual runs by construction.
- Existing `ports/` Protocols — `EvalRepo` and `Scorer` are NEW
  Protocols; no existing one changes.

## Consequences

- Scoring is read-path work: the first detail GET after completion
  does N scorer passes (deterministic, fast; the judge adds one model
  call per case) and pays one persist. Large datasets may make that
  first read slow — acceptable at v1 scale; a scoring job is the
  deferred future path.
- Datasets are versioned by snapshot (the eval_run row), not by
  dataset versioning — dataset edits do not retroactively change past
  runs' meaning.
- `EvalObservation` is deliberately small (status, final message, tool
  order). richer observations (tool outputs, usage, latency) arrive
  when a scorer needs them — the port's shape makes that additive.
- The UI gains a third execution-adjacent surface; the executions
  listing needs no eval-awareness (metadata carries the linkage).