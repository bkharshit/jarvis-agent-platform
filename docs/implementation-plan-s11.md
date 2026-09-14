# Implementation plan — S11: evaluation framework (ADR 0017)

Stage: S11. Format follows `docs/implementation-plan-s12.md`. Every
commit below is independently green: `pytest tests/unit`, `ruff check
src tests` + format, `mypy src` — and per web commit `tsc --noEmit`,
eslint, `vitest`. Integration commits additionally pass
`uv run pytest tests/integration -q -m db` (local Postgres, no Docker).

Scope (ADR 0017): datasets (JSONB case snapshots), eval runs as
ordinary child agent runs through the existing queue, deterministic
scorers + llm_judge, lazy persist-once scoring, version comparison,
capabilities flip, web Evaluations section. The worker and the event
envelope are untouched.

**Testing note (Harshit, 2026-09-15):** S11's manual-testing session
happens TOGETHER with S12's (one joint session, `docs/walkthrough-s12.md`
+ a new `docs/walkthrough-s11.md` as scripts) — the closure commit
lands after that joint session, not after this build alone.

## Verified seams (read in code, 2026-09-15)

- `api/routes/_run_routes.py` `queue_message()` (run_id at enqueue;
  metadata + kind fields exist) / `queued_result()` / `await_segment`.
- `persistence/repositories.py:681` `create_queued_run` — row+message
  ONE tx; `:853` `list_runs` — NO metadata filter (drives eager
  child-run_id storage, D48).
- `runtime/worker.py:245` resolves by `message.agent_version_id` —
  pinned children need no worker change; `:54` `AgentVersionLoader`
  structural protocol.
- `api/routes/agents.py:62` run route pins `latest_version()` at
  enqueue — the eval-run create pins the same way.
- `ports/repository.py` `ExecutionRepo.list_tool_executions(run_id)`
  (scorer observation source); `AgentRepo` has NO `get_version_by_id`
  on the port (the SQL repo implements it for the worker — the eval
  route resolves versions through the agent route's seam, not a new
  port method).
- `persistence/scoped.py:130` `TenantScopedExecutions.create_queued_run`
  — the tenant-scoped enqueue the eval runner reuses.
- `api/routes/capabilities.py` `_SECTION_FLAGS["evaluations"]` already
  exists (disabled, stage S11); detail-derivation pattern per key.
- Web: `sectionRegistry.ts:35` (`evaluations`, route `/evaluations`);
  `app/routes.tsx:68` gated placeholder — the section activates.
- `domain/agent.py:34` `ModelRef` (judge_model shape);
  `api/routes/models.py:41` `ApiError(422, "validation", ...)` — the
  dataset-validation convention.
- `persistence/migrations/versions/` — 0001..0010; next is 0011.
- `models/mock.py` scripted turns — child-run scoring is unit-testable
  end to end without a DB or a real model.

## Confirmed decisions (ADR 0017 carries the contract)

### D48 — three tables; cases as JSONB snapshots on dataset AND run

`eval_datasets` (cases/scorers/judge_model JSONB), `eval_runs`
(dataset JSONB snapshot + pinned agent_version_id), `eval_results`
(case_id, run_id, scores JSONB, error, scored_at). No separate
`eval_cases` table (deviation from the sketch, recorded in ADR 0017
§2): cases are read whole, never individually; a stable per-case uuid
inside the JSONB replaces a case FK. Child run_ids are stored eagerly
at create (no metadata-filtered child query exists or is needed);
child metadata `{"eval": {...}}` is traceability only. Tenancy via
explicit `tenant_id` kwargs (McpServerRepo pattern, D29).

### D49 — status derived at read; scoring lazy, persisted once

No status column on eval_runs: the detail read derives
running/completed from the child run rows' statuses. Scoring fires on
the first completed detail read (observation = final message + tool
order via `list_tool_executions`), scorers run against the dataset
SNAPSHOT, results persist once (`scores` + `scored_at`); re-evaluation
is a new eval run. A failed child is scored honestly against its
observation — no special-casing. An `awaiting_input` child (HIL agent)
keeps the eval "running" — recorded limitation.

### D50 — Scorer port; five deterministic scorers; llm_judge honest

`ports/scoring.py`: `Scorer` Protocol (`name`, `score(case,
observation) -> Score`). Shipped adapters: exact / contains / regex /
json_schema / tool_sequence (all deterministic, unit-tested pure) +
`llm_judge` — ONE `generate()` through the model factory resolving the
dataset's `judge_model` (D28 pattern; no loop, no streaming). Judge
failure (ModelError or unparseable verdict) persists `passed=null` +
error detail — never a retry, never a fabricated pass. A dataset with
`llm_judge` in scorers but no `judge_model` is rejected 422 at
create/update — an invalid dataset can never be snapshotted.

## Commit sequence

1. **Planning** (this commit): ADR 0017 + this plan + D48–D50 in
   decisions.md + roadmap S11 planning record. No S11 code.
2. **Domain + ports**: `domain/evaluation.py` (EvalCase, ScorerConfig,
   EvalDataset, EvalRun, Score, EvalResult, EvalObservation);
   `ports/repository.py` `EvalRepo` Protocol; `ports/scoring.py`
   `Scorer` Protocol. Unit: domain-model parse (extra=forbid shapes,
   defaults), ports typing.
3. **Persistence**: `SqlEvalRepo` + migration 0011 (three tables,
   UNIQUE(eval_run_id, case_id)) + integration tests (dataset CRUD +
   tenant scoping 404s, create_run snapshot + results one-tx, list
   filters, save_scores idempotent write).
4. **Deterministic scorers + observation**: `scoring/` adapters
   (exact/contains/regex/json_schema/tool_sequence) + the
   observation-builder (run row + tool executions → EvalObservation).
   Unit: each scorer's pass/fail edge (trim, multiline regex, invalid
   JSON, wrong tool order), observation builder with a fake repo.
5. **llm_judge + eval-run creation**: the judge scorer (model-factory
   resolution, verdict parse, honest failure) + the create-run service
   (validate 422, pin version, per-case `queue_message` +
   `create_queued_run`, `create_run` one-tx) + unit tests (mock
   provider children, judge verdict parse, judge ModelError →
   passed=null + error detail, 422 missing judge_model, metadata
   stamps).
6. **API routes + capabilities**: `/v1/evaluations*` routes (datasets
   CRUD, run create 202, run list/detail with derived status + lazy
   scoring, compare) + `evaluations` flip with derived detail counts.
   Integration: dataset 422, eval run of a mock agent → children
   queued with metadata, detail derives status and scores persist
   once (second GET does not re-score), tenant 404s, compare
   aggregation.
7. **Web**: gen-api regen; Evaluations section (datasets list/editor,
   dataset detail, eval-run detail with per-case scores, version
   comparison view); vitest. Gates: tsc, eslint, vitest.
8. **Joint manual session + closure** (post-build, Harshit): run
   `docs/walkthrough-s12.md` + `docs/walkthrough-s11.md` live TOGETHER
   in one session; then the closure commit — walkthroughs with session
   notes, README evaluation section, roadmap S11 shipped note,
   CLAUDE.md gotchas from the build.

## Rule-7 audit (what touches what)

| Touch | File(s) | Frozen-surface? |
| --- | --- | --- |
| `EvalDataset`/`EvalCase`/`ScorerConfig`/`Score`/`EvalResult`/`EvalObservation` | `domain/evaluation.py` (new) | new pure-Pydantic types |
| `EvalRepo` (new Protocol) | `ports/repository.py` | new Protocol — not a change to an existing one |
| `Scorer` (new Protocol) | `ports/scoring.py` (new) | new Protocol |
| migration 0011 | `persistence/migrations/versions/` | three new tables |
| `SqlEvalRepo` | `persistence/repositories.py` | new adapter, explicit tenant_id kwargs |
| eval-run create service | `api/routes/evaluations.py` | composes `queue_message` + `create_queued_run` unchanged |
| capabilities | `api/routes/capabilities.py` | one flag flips; detail derived from the repo (S4 pattern) |
| worker / queue / envelope | — | **zero changes** |
| `AppContainer` | `api/deps.py` | one field (`evaluations: SqlEvalRepo`) |

## Acceptance (roadmap S11, adapted to the shipped scope)

- An eval run of a mock agent produces scores persisted on
  `eval_results` and comparable across agent versions (compare
  endpoint groups by `agent_version_id`).
- An eval run's children are indistinguishable from manual runs in
  `/executions` — same event model, same row shape, eval linkage only
  in `metadata`.
- A dataset with `llm_judge` but no `judge_model` is rejected 422; a
  judge failure stores `passed=null` + error detail honestly.
- The detail read's derived status flips running → completed with the
  children; scoring happens once (idempotent across reads).
- A foreign tenant's dataset/eval run reads as absent (404), never
  leaked.