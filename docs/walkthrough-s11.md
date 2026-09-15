# S11 manual walkthrough — evaluations (ADR 0017), live script

A hands-on reproduction of the S11 acceptance behavior against a live
stack. Format follows walkthrough-s12 (and is tested in the SAME joint
session — run that §0 setup first). Backend :8001, web :5173, dev DB
`jarvis` (migration 0011 adds `eval_datasets` / `eval_runs` /
`eval_results`), mock agents for the deterministic steps, real
`gemma4:31b` for the judge steps.

S11 in one sentence: **evaluations are test-case datasets scored over
ordinary runs** — a dataset is a JSONB snapshot (D48), an eval run fans
out one ORDINARY queue run per case pinned to the agent's latest
published version, scores are computed lazily on the first completed
detail read and persisted ONCE (D49), and the scorer set is five
deterministic scorers + `llm_judge`, which requires a dataset-level
judge model at the create/update boundary (D50).

The three invariants the walkthrough exercises:

- **D48 — the snapshot rides the run.** Editing the dataset after a
  run never changes what that run's results refer to; the detail read
  re-sorts results into the SNAPSHOT's case order, not the current one.
- **D49 — status is derived, scoring is lazy + persist-once.** There is
  no status column: any non-terminal child means "running". The first
  completed read scores the unfinished results; a later read never
  re-scores (`scores IS NULL` is the guard) — `scored_at` is frozen.
- **D50 — judge failures are honest, never fatal.** `llm_judge`
  without a dataset `judge_model` is a 422 at the dataset boundary; a
  judge that cannot produce a verdict persists `passed=null` + detail —
  the run is long done, the score just has no verdict.

## 0. Setup

The S12 walkthrough's §0 covers the stack; this stage adds migration
0011. If starting fresh here:

```bash
lsof -nP -iTCP:8001 -sTCP:LISTEN      # kill any stale backend first
uv run alembic upgrade head           # migrations 0010 + 0011
uv run jarvis serve > /tmp/jarvis-8001.log 2>&1 &
sleep 3; curl -s localhost:8001/healthz
export JARVIS_KEY="jarvis_sk_…"       # your existing api key
export AUTH="Authorization: Bearer $JARVIS_KEY"
```

Sanity — the section flag flipped with commit 1868c5b:

```bash
curl -s -H "$AUTH" localhost:8001/v1/capabilities \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["sections"]["evaluations"])'
# → {"enabled": true, "summary": "Datasets, runs, and scores", "detail": {"datasets": 0, "runs": 0}}
```

## 1. A dataset — create, read, wholesale PATCH

Two cases whose expected matches the mock's default reply, plus one
that cannot match (a built-in deterministic demonstration of pass AND
fail — see the mock-FIFO note in §2):

```bash
curl -s -H "$AUTH" -H "Content-Type: application/json" \
  localhost:8001/v1/evaluations/datasets -d '{
  "name": "smoke-dataset",
  "description": "S11 joint-session demo",
  "cases": [
    {"id": "c-pass", "input": "say hi", "expected": "This is a mock response."},
    {"id": "c-fail", "input": "say hi", "expected": "hi"}
  ],
  "scorers": [{"name": "exact"}]
}' | python3 -m json.tool | grep '"id"'
export DS="<id from above>"
```

PATCH wholesale (the PATCH discipline — name AND cases AND scorers go
as ONE payload on EVERY PATCH; a name-only body 422s. Here just rename,
proving the snapshot is per-RUN later):

```bash
curl -s -X PATCH -H "$AUTH" -H "Content-Type: application/json" \
  localhost:8001/v1/evaluations/datasets/$DS -d '{
  "name": "smoke-dataset-v2",
  "cases": [
    {"id": "c-pass", "input": "say hi", "expected": "This is a mock response."},
    {"id": "c-fail", "input": "say hi", "expected": "hi"}
  ],
  "scorers": [{"name": "exact"}]
}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["name"])'
# → smoke-dataset-v2
```

## 2. Run it — children are ORDINARY runs (the core S11 fact)

Pick any agent (the mock agents from walkthrough-s12 §1 work; mock +
function_calling per the D19 smoke rule) — note its id, then:

```bash
curl -s -H "$AUTH" -H "Content-Type: application/json" \
  localhost:8001/v1/evaluations/datasets/$DS/runs \
  -d '{"agent_id": "<agent_id>"}' | python3 -m json.tool
# → 202, status "running", id = the eval run
export EVALRUN="<id from above>"
```

Immediately — the run's children are in the ordinary executions list,
indistinguishable from any other run by construction:

```bash
curl -s -H "$AUTH" 'localhost:8001/v1/executions?status=queued' \
  | python3 -c 'import json,sys; [print(r["run_id"], r["metadata"].get("eval")) for r in json.load(sys.stdin)["items"]]'
# → one run per case, metadata {"eval": {"eval_run_id": …, "case_id": …}}
```

**Mock-FIFO note (why the expecteds above are chosen the way they
are):** eval children execute CONCURRENTLY — the worker claims each
case as its own asyncio task — and the mock provider's scripted turns
are one global FIFO, so per-case scripted answers are unreliable on
multi-case datasets. The `exact` scorer against the mock's default
reply ("This is a mock response.") is the deterministic demo: c-pass
scores 1.0, c-fail scores 0.0, both honestly.

## 3. Read the detail — lazy scoring, persist-once

First completed read scores + persists:

```bash
curl -s -H "$AUTH" localhost:8001/v1/evaluations/runs/$EVALRUN \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["status"], [(r["case_id"], r["scores"], r["scored_at"]) for r in d["results"]])'
# → completed; c-pass: exact passed=True score=1.0 detail="matched"
#            c-fail: exact passed=False score=0.0
```

Read it AGAIN — the receipt that scoring never re-runs:

```bash
curl -s -H "$AUTH" localhost:8001/v1/evaluations/runs/$EVALRUN \
  | python3 -c 'import json,sys; print([r["scored_at"] for r in json.load(sys.stdin)["results"]])'
# → identical scored_at values (a second read never re-scores)
```

DB receipt (JSONB snapshot + one row per case, D48):

```bash
psql "postgresql://jarvis:jarvis@localhost/jarvis" \
  -c "SELECT case_id, run_id, scores IS NOT NULL AS scored, scored_at FROM eval_results WHERE eval_run_id='$EVALRUN' ORDER BY case_id"
```

And the D48 proof: the run detail's dataset.name is the value AT RUN
CREATION… (actually the snapshot carries the name the run saw —
rename BEFORE the run in §1 made it smoke-dataset-v2; rename again
AFTER the run and re-GET the run detail to see the snapshot holding
its own copy):

```bash
curl -s -X PATCH -H "$AUTH" -H "Content-Type: application/json" \
  localhost:8001/v1/evaluations/datasets/$DS -d '{
  "name": "renamed-after-run",
  "cases": [
    {"id": "c-pass", "input": "say hi", "expected": "This is a mock response."},
    {"id": "c-fail", "input": "say hi", "expected": "hi"}
  ],
  "scorers": [{"name": "exact"}]
}' > /dev/null
curl -s -H "$AUTH" localhost:8001/v1/evaluations/runs/$EVALRUN \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["dataset"]["name"])'
# → smoke-dataset-v2 (the snapshot) while GET /datasets shows renamed-after-run
```

## 4. llm_judge — 422 at the boundary, a real verdict, an honest no-verdict

First the 422 — llm_judge without a dataset judge_model (D50; the body
still carries the full wholesale payload — a missing-cases 422 would
fire for the wrong reason):

```bash
curl -s -X PATCH -H "$AUTH" -H "Content-Type: application/json" \
  localhost:8001/v1/evaluations/datasets/$DS -d '{
    "name": "renamed-after-run",
    "cases": [
      {"id": "c-pass", "input": "say hi", "expected": "This is a mock response."},
      {"id": "c-fail", "input": "say hi", "expected": "hi"}
    ],
    "scorers": [{"name": "exact"}, {"name": "llm_judge"}]
  }' | python3 -m json.tool
# → 422 validation, errors[] names the judge_model requirement
```

Now WITH a judge — the real gemma endpoint (S12 pattern: env-ref'd
key, never stored material in the request):

```bash
curl -s -X PATCH -H "$AUTH" -H "Content-Type: application/json" \
  localhost:8001/v1/evaluations/datasets/$DS -d '{
    "name": "renamed-after-run",
    "cases": [
      {"id": "c-pass", "input": "say hi", "expected": "This is a mock response."},
      {"id": "c-fail", "input": "say hi", "expected": "hi"}
    ],
    "scorers": [{"name": "exact"}, {"name": "llm_judge"}],
    "judge_model": {"provider": "openai_compatible", "model": "gemma4:31b",
                    "base_url": "https://ollama.com/v1",
                    "credential_ref": {"type": "env", "env_var": "OLLAMA_API_KEY"}}
  }' | python3 -c 'import json,sys; print(json.load(sys.stdin)["judge_model"]["model"])'
# → gemma4:31b
```

Run a second evaluation and open its detail: the judge scores the
child's final answer ("This is a mock response.") against expected
"hi" — expect an honest FAIL with the judge's reasoning in `detail`.
The judge call happens on the READ path (the first completed detail
read), OUTSIDE any run — so the child run's llm-trace shows only the
agent's own call (verified live: the child trace's only prompt is the
case input "say hi"; the judge prompt appears in no run's trace):

```bash
curl -s -H "$AUTH" -H "Content-Type: application/json" \
  localhost:8001/v1/evaluations/datasets/$DS/runs -d '{"agent_id": "<agent_id>"}'
# then GET the new run detail (this read scores); the child's llm-trace
# holds ONLY the agent loop call — the judge call belongs to no run
```

Judge failure honesty, live: PATCH the judge_model to a dead endpoint,
run again, and read — the judge errors at scoring time and the result
persists `passed=null` + a `detail` explaining, while the run itself
stays succeeded (the GET may pause a few seconds while the dead judge
times out — scoring happens on that read):

```bash
# PATCH judge_model to {"provider": "openai_compatible", "model": "x",
#                       "base_url": "https://localhost:9", "credential_ref": {"type": "env", "env_var": "ANY_KEY"}}
# run + GET detail → llm_judge chip "no verdict", detail names the failure
```

## 5. Web — the Evaluations section

Open http://localhost:5173 → **Evaluations** (capability-gated, now
enabled):

- Datasets table + **New dataset** → starter dataset (one case, exact
  scorer) → lands on the dataset editor.
- Editor: rename, edit cases (add `c-1`-style rows, remove), add a
  scorer via the select (the hint line names its params; choosing
  llm_judge without a judge model warns AND the backend 422s on save —
  the banner relays the message verbatim), Save changes → toast.
- **Run evaluation**: pick an agent → Run → navigates to the eval-run
  detail; the status chip flips running → completed by polling (D49's
  derived status — there is no status column for it to read).
- Detail: per-case cards with score chips — green "pass (1)", red
  "fail (0)", neutral "no verdict" for judge failures — each linking
  its CHILD run into Executions, where it is an ordinary run.
- **Compare** (/evaluations/compare): pick the agent → one row per
  pinned agent_version with cases/scored/pass-rate and per-scorer
  chips. Publish a v2 first (Agents → edit → save publishes version 2),
  run an eval on it, and compare shows BOTH versions side by side.

## 6. Delete protection + what is unit-covered, not live-scripted

DELETE with runs → 409, verbatim:

```bash
curl -s -X DELETE -H "$AUTH" localhost:8001/v1/evaluations/datasets/$DS \
  | python3 -m json.tool
# → 409 conflict "dataset has eval runs; delete them first"
```

- **Tenant scoping (D29)**: another tenant's dataset/run reads as 404
  (eval_results has no tenant column — scoping joins through the run).
  Covered in test_api_evaluations.py; making it live needs a second
  tenant session — skip unless curious.
- **Judge resolution per read (D28-on-the-read-path)**: the judge model
  resolves with the READER's principal, so a credential visible to the
  run's tenant but not the reader degrades to no-verdict — unit-covered.

## 7. Teardown / receipts

```bash
psql "postgresql://jarvis:jarvis@localhost/jarvis" \
  -c "SELECT eval_run_id, count(*), count(scores) FROM eval_results GROUP BY 1"
# one row per (eval_run, case) — counts match the datasets' case counts
curl -s -H "$AUTH" localhost:8001/v1/evaluations/runs | python3 -m json.tool | grep -c '"id"'
# the runs from §2/§4 remain; delete the datasets after (empty ones delete cleanly)
```

## 8. Session notes — findings from the live run (2026-09-15)

The joint S12+S11 session ran this script live. Deviations and finds,
in the order they happened:

1. **Wholesale PATCH, again (this doc was wrong twice).** Both §1 and
   §3's rename-only PATCH bodies 422'd live — `EvalDatasetUpsert`
   requires name + cases + scorers on EVERY PATCH. The examples above
   now carry the full payload. Rule of thumb: a PATCH body is a full
   dataset document, never a diff.
2. **Live bug → fix d617749 — empty case input 500'd run creation.**
   The web "New dataset" starter case shipped `input: ""`, which
   sailed through dataset create and then escaped as a pydantic
   ValidationError → 500 at POST /runs (RunRequest.input is
   min_length=1). Fix: `EvalCase.input = Field(min_length=1)` so the
   dataset boundary rejects it (422), plus a regression test (POST and
   PATCH) and a required "First case input" field in the web create
   form. Lesson: an inner type's constraint must be enforced at the
   outermost boundary that accepts the data.
3. **Live outage during that fix — one poisoned JSONB row bricked the
   whole shell.** The pre-fix starter case (`input: ""`) persisted as a
   dataset row; the new min_length=1 made `_dataset` raise on READ,
   which bubbles: list_datasets → `_evaluations_detail` → GET
   /v1/capabilities 500 → every section shows "Cannot reach the
   JARVIS backend". The S4 derived-facts gotcha in a new costume:
   **tightening a domain constraint on a JSONB-persisted type makes
   pre-existing rows unparseable** — a constraint change needs a story
   for existing rows (repair/lenient read), because capabilities
   derives its detail counts from full listings. Repaired with
   `DELETE FROM eval_datasets WHERE EXISTS (SELECT 1 FROM
   jsonb_array_elements(cases) c WHERE c->>'input' = '')`.
4. **The judge panel collects provider + model only** — no base_url /
   credential_ref inputs, so the real gemma judge is curl-only from
   the web. The editor's wholesale save originally DROPPED those
   uncollected fields (silent data loss on first web save); fixed in
   ca4b4a5 — the original ref rides the draft, the PATCH rebuilds from
   it, and uncollected fields render read-only. Full judge-panel
   support (base_url + credential-ref inputs) is a roadmap follow-up.
5. **Judge vs exact on identical outputs** (the §4 comparison, run
   live as dataset "judge-vs-exact": 3 cases, both scorers, gemma as
   judge). Receipt — exact and llm_judge DISAGREE exactly where a
   string comparison can't judge semantics, on the same output
   "This is a mock response.":
   - expected = the exact reply → both pass;
   - expected = a different greeting → both fail;
   - expected = "a reply written in English" → exact FAILS (strings
     differ), judge PASSES ("The agent's response is written in
     English, which satisfies the expected answer criteria.").
6. **422 toasts relay verbatim** through the dataset editor banner
   (screenshot receipt: "scorer 'llm_judge' requires judge_model on
   the dataset").