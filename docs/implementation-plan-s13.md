# Implementation plan — S13: triggers (ADR 0019)

Stage: S13. Format follows `docs/implementation-plan-s14.md`. Every
commit below is independently green: `pytest tests/unit`, `ruff check
src tests` + format, `mypy src` — and per web commit `tsc --noEmit`,
eslint, `vitest`. Integration commits additionally pass
`uv run pytest tests/integration -q -m db` (local Postgres, no Docker).

Scope (ADR 0019, confirmed by Harshit 2026-09-16): all three trigger
kinds in v1 — cron schedules (croniter + a Postgres-truth scheduler
loop), webhooks (unauthenticated HMAC ingest, per-trigger write-only
secret), event watchers (closed condition set, finalize-choke-point
observer, depth guard). A trigger is a queue producer; no envelope,
queue-contract, or worker-claim changes.

## Verified seams (read in code, 2026-09-16)

- `runtime/child_runs.py:29-86` — `child_message` (pre-generated
  `uuid4` run_id) + `enqueue_child_run` (`create_queued_run`, one tx,
  ADR 0008 §2): the firing composition to transpose (no parent).
- `persistence/repositories.py:2081-2108` — the queue `claim`
  (subquery `FOR UPDATE SKIP LOCKED` + guarded `UPDATE … RETURNING`),
  the codebase's only concurrency idiom; guarded decision writes
  `requeue`/`ack` carry `WHERE status='claimed'` (`:2021-2123`).
- `runtime/worker.py:451-454` `_sweep_loop` — the periodic-loop
  idiom; `api/deps.py:229-252` `start_worker`/`stop_worker` — the
  container-managed background-task pattern; lifespan gate at
  `api/app.py:37-50`.
- `persistence/repositories.py:827-847` — `SqlExecutionRepo.finish_run`
  is the ONE choke point every terminal path funnels through (runtime
  tails `agent_runtime.py:261-263`/`:852-854`,
  `workflow_runtime.py:243-245`/`:315-317`; worker fail-fast
  `worker.py:189, 213, 256`; sweeper `worker.py:421` +
  `_finish_from_terminal` `:473-502`; `finish_paused_run`
  `worker.py:556-588`, shared with the cancel route
  `routes/executions.py:150`). **`agent_runtime._result`
  (`:1184-1213`) builds the finalize RunResult WITHOUT row metadata —
  the observer must read the ROW's metadata, never the passed result
  (ADR 0019 §5).**
- `domain/auth.py:16-35` — `AuthMode = Literal["anonymous", "session",
  "api_key"]`; `Principal` (`tenant_id, user_id, api_key_id, role,
  mode`); `mode`'s only branch is `is_anonymous` — the additive
  `"system"` member is safe (ADR 0019 §2).
- `tools/subagents/provider.py:147-167` — child/successor metadata is
  MINTED fresh (`{"sub_agent": …, "auto_decline_approvals": True}`);
  no parent-namespace copy — the §5 pre-declared change site.
- `api/auth.py:79-122` — no auth middleware; a route without `AuthDep`
  is unauthenticated (`/v1/auth/login` precedent). Anonymous-mode
  full-access + required-mode role gates: the mcp `_require_manager`
  pattern (`routes/mcp.py:30-34`).
- `security/crypto.py` — AES-256-GCM envelope `encrypt`/`decrypt`
  (key_id fingerprint, master key by env-var name); write-only read
  model + 503 `credentials_unavailable`:
  `routes/credentials.py:24-48`; plaintext-once: API keys
  (`routes/auth.py`).
- `domain/workflow.py:275-302` — `render_template(template, values)`,
  `_TEMPLATE_PATTERN` flat vars, unknown pass through.
- `api/routes/_run_routes.py:42-89` — `queue_message` metadata stamping
  (workflow-kind precedent `:57-59`); `routes/executions.py:70-90` —
  the `parent_run_id` filter precedent (S14) to mirror for
  `trigger_id`; `persistence/repositories.py:863-889` `list_runs`
  where-clause; `persistence/scoped.py:127-168` the tenant view.
- `routes/capabilities.py:39-55, 148-181` — `_SECTION_FLAGS`
  (`"triggers"` disabled entry exists, `:53`) + derived-detail
  pattern; **the S4 lesson: the detail branch ships WITH migration
  0013 in the same commit** or every `GET /v1/capabilities` 500s.
- Migrations `0001..0012` (next: `0013`, header pattern
  `0012_parent_run_id.py:12-17`); tenant-owned table pattern
  `0011_eval_tables.py:31-37, 55-61`; native enum emission gotcha D4
  (let `op.create_table` emit CREATE TYPE).
- `config.py:9-89` — pydantic-settings knobs (new fields = new
  `JARVIS_*` env names, no registration); uniformly UTC.
- Web: `sectionRegistry.ts` (`triggers` entry exists), routes.tsx
  placeholder `:81`, `useCapabilities`, msw `handlers.ts` (flip the
  `triggers` capability fixture), gen-api via `make gen-api`
  (`scripts/gen_openapi.py`, no DB).
- `models/mock.py` — the whole stage is unit-testable with fake
  repos/queue; integration uses mock-provider agents on `*/1`-style
  schedules.

## Confirmed decisions (ADR 0019 carries the contract)

### D57 — one tenant-owned `triggers` table; three kinds; typed configs

Migration 0013: `triggers` (eval tenancy pattern) + kind-specific JSONB
`config` union (`CronConfig{schedule}`, `WebhookConfig{}`,
`EventConfig{status, source_agent_id?}` — domain/trigger.py, closed
set) + `agent_executions.trigger_id` nullable string, NO FK, + index
(queryable provenance filter; no FK so deleted triggers' runs keep
provenance — DELETE is always 204, the eval 409-with-runs REJECTED).
`ports/repository.py`: NEW `TriggerRepo` Protocol (explicit
`tenant_id` kwargs, the McpServerRepo pattern — no scoped.py wrapper)
+ `ExecutionRepo.list_runs` gains `trigger_id` keyword. Version
policy: `pinned` = route-stamped `agent_version_id` (the
`_pin_agent_versions` pattern; 422 no published version; republish =
detail LINT), `latest` = NULL pin, resolved at fire time (explicit
opt-in to drift, no lint).

### D58 — a trigger is an ordinary queue producer; the service principal

`runtime/trigger_firing.py` `TriggerFireService.fire()` composes
`child_message` with NO parent: fresh run_id + trace_id, fresh platform
deadline (`deadline_from_now(run_timeout_seconds)`), `session_id=None`,
`metadata={"trigger": {trigger_id, kind, name, depth}}`, then
`enqueue_child_run`. Principal =
`Principal(tenant_id=trigger.tenant_id, mode="system")` — the NEW
additive `AuthMode` member (D28 credential resolution needs a tenant;
`None` breaks stored BYOK; `is_anonymous` is `mode`'s only branch).
Input = `render_template(input_template, vars)`; per-kind honest
defaults when empty (cron: "Scheduled run … by trigger '<name>' at
<now>"; webhook: the raw payload capped; event: "Run <run_id> … status
<status>"); caps from Settings. Memory-on-target = detail lint
(`memory_ignored` pattern). Depth: cron/webhook fire at depth 0, event
at incoming+1 (D61 reads it).

### D59 — cron: croniter + Postgres-truth loop; claim-first at-most-once

`croniter>=2.0` (five-field expressions, UTC-only; per-trigger IANA
timezones deferred). `runtime/scheduler.py`: `sleep(trigger_poll_seconds)
; fire_due()` loop, exceptions contained (loop never dies);
`AppContainer.start_scheduler`/`stop_scheduler` (the worker pattern),
serve-lifespan-gated by `trigger_scheduler_enabled: bool = True`
(independent of `embedded_worker`); NOT in the `jarvis worker` CLI.
`TriggerRepo.claim_due_crons(now, limit)`: one tx — SELECT due rows
`FOR UPDATE SKIP LOCKED`, advance each (`last_fired_at=now`,
`next_fire_at=croniter_next(now)`, `last_error=NULL`), COMMIT; the
enqueue happens AFTER the claim commit, per trigger; enqueue failure →
`last_error` on the row (at-most-once; a missed tick is benign, a
duplicate side-effect run is not). Catch-up = fire ONCE (all math
`croniter(schedule, now)`); create/re-enable stamps
`next_fire_at=croniter_next(now)`; disable clears it; the claim
requires `enabled`.

### D60 — webhooks: unauthenticated HMAC ingest; per-trigger write-only secret

`POST /v1/triggers/webhook/{trigger_id}` — NO `AuthDep` (no middleware
exists); the trigger row is the trust boundary (tenant/principal NEVER
from the request). Signature: `X-Jarvis-Signature: sha256=<hex hmac of
raw body>`, constant-time compare. Responses: missing / non-webhook /
disabled → 404 (absent, D29 stance); bad signature → 401
`invalid_signature`; body > `trigger_webhook_max_body_bytes` (64 KiB)
→ 413; else enqueue with `{{payload}}` (raw body, utf-8
errors="replace", capped) → 202 `{run_id, status: "queued"}` (never
blocks on the run; it streams via the existing executions endpoints).
Secret: server-generated `token_urlsafe(32)` at create, AES-GCM envelope
in `triggers.secret`; plaintext EXACTLY ONCE (create + rotate
responses); never in GET/logs (D29/D30); missing master key at
create/rotate → 503 `credentials_unavailable`. Replay protection
deferred.

### D61 — event triggers: finish_run post-commit observer; depth guard

`SqlExecutionRepo.__init__` gains optional
`on_finalized: Callable[[RunResult], Awaitable[None]] | None`;
`finish_run` invokes it AFTER commit, best-effort (try/except + log —
a trigger side-effect can NEVER break a terminal write, rule 2), and
passes the result rebuilt from the ROW (status AND `row.metadata`) —
the runtime's `_result` carries no row metadata, and the guard must
read PERSISTED provenance. Container wires the callback to
`TriggerService.on_run_finalized` (deps.py). Guard: skip when the
finalized run's own `metadata["trigger"]["depth"] >=
trigger_max_depth` (default 1). Pre-declared S14 seam change: BOTH
`tools/subagents/provider.py` producers (child, handoff successor)
copy the parent's `metadata["trigger"]` verbatim when present —
without it a child-completion re-fires through every generation;
regression-gated byte-identical for provenance-free parents.
Eval/sub-agent/handoff children ARE ordinary runs and DO fire event
triggers (bounded by the guard). Failures log + `last_error` on the
trigger; the finalized run is never affected.

### D62 — provenance + API + capabilities + UI

Row `trigger_id` (filter) + `metadata["trigger"]` namespace (display —
the sketch's flat `metadata.trigger_id` namespaced like every other
producer). Routes `/v1/triggers` (mcp CRUD pattern; full-document
PATCH; role gates; foreign 404): list, detail (+`next_fire_at`,
`last_fired_at`, `last_error`, lints; NO secret), create (validates
config, stamps pins, secret-once for webhook), patch (recompute
`next_fire_at` on enable), delete (204 always), rotate-secret,
fire-now (409 when disabled — "disabled fires nothing" stays true),
`GET /v1/executions?trigger_id=`. Capabilities `triggers.enabled` +
derived detail (tenant counts by kind) — SAME commit as the 0013 usage
(S4 lesson). Web: Triggers section (list, per-kind editor, toggle,
fire-now, webhook URL + secret-once + rotate, runs-of-trigger list),
ExecutionDetail provenance chip + link; msw fixtures flip.

## Commit sequence

1. **Planning** (this commit): ADR 0019 + this plan + D57–D62 in
   decisions.md + roadmap S13 planning record. No S13 code.
2. **Domain + ports + persistence**: `uv add croniter`; NEW
   `domain/trigger.py` (Trigger + config union + kind literal);
   `domain/auth.py` `AuthMode` gains `"system"` (additive);
   `ports/repository.py` TriggerRepo Protocol + `list_runs` trigger_id
   keyword; `SqlTriggerRepo` (+ claim_due_crons / record_fire /
   enabled_event_triggers); `SqlExecutionRepo.list_runs` filter +
   scoped view keyword; migration 0013 (triggers + native
   `trigger_kind` enum — let `op.create_table` emit, D4 — +
   `agent_executions.trigger_id` + index). Unit: domain config
   validation (cron shape via croniter, status literals, union
   discrimination), AuthMode parse, list_runs keyword default no-op
   (byte-identical). Integration: table round-trip, claim exclusivity
   (two racing claimers → one winner), filter, tenant scoping.
3. **Firing service + version stamping**: `runtime/trigger_firing.py`
   (version resolution pinned/latest — latest → current published
   version, honest miss error; template render + per-kind defaults +
   caps; `child_message` composition with the service principal;
   trigger metadata namespace + depth). S14 copy:
   `tools/subagents/provider.py` child + successor metadata gains the
   parent's `trigger` namespace when present (regression-gated:
   absent → byte-identical). Unit: resolution branches, defaults per
   kind, unknown-vars pass-through, depth stamp, S14 copy
   present/absent, principal shape (`mode="system"`, tenant from
   trigger).
4. **Scheduler**: `runtime/scheduler.py` loop + `Settings` knobs
   (`trigger_scheduler_enabled=True`, `trigger_poll_seconds=5.0`,
   `trigger_max_depth=1`, `trigger_webhook_max_body_bytes=65536`,
   `trigger_event_max_output_chars=8000`) + container
   start/stop_scheduler + serve lifespan wiring. Unit: fire_due claims
   once (fake repo + injected clock), exception containment, catch-up
   fire-once (past next_fire_at → exactly one fire, next from now),
   enqueue-failure → last_error (claim committed, no re-fire).
5. **API CRUD + fire + rotate + capabilities**: `routes/triggers.py`
   (mcp pattern; full-document PATCH; role gates; 422s — bad cron,
   no published version; 409 disabled fire; 404 foreign D29; secret
   created+returned-once for webhook kind, never in GET; missing
   master key 503), schemas in `api/schemas.py`; `GET
   /v1/executions?trigger_id=`; capabilities flip + derived detail
   (**same commit as 0013 usage — integration proves capabilities
   derives from real rows**); app.py registration. Integration: CRUD
   round-trip, 422 matrix, role/tenant matrix, secret-never-in-GET,
   capabilities derived counts, enable-toggle recomputes next_fire_at.
6. **Webhook ingest**: the unauthenticated route + HMAC verify
   (constant-time; sha256=<hex>) + body cap + 404/401/413 semantics +
   `{{payload}}` input + 202 queued. Unit: HMAC matrix (valid /
   tampered body / wrong secret / missing header / disabled / missing
   trigger / non-webhook kind), 413, enqueue composition, no-AuthDep
   (route constructs nothing from the request principal).
   Integration: signed curl-equivalent POST → 202 run_id → embedded
   worker claims → terminal event → run visible in executions with
   trigger_id + metadata namespace; tampered → 401 and NO run row.
7. **Event triggers**: `on_finalized` observer param + row-rebuilt
   payload + best-effort invoke; container wiring; `on_run_finalized`
   (terminal check, EventConfig match incl. source_agent filter +
   `any`, depth guard reading ROW metadata, enqueue via the firing
   service, last_error). Unit: observer swallows exceptions (finalize
   unaffected), condition matrix, depth refusal, row-metadata
   authority (passed result with contradicting metadata → guard uses
   row), child-completion fires (provenance copied, D61). Integration:
   a succeeded run → event trigger fires target → target run claimed +
   succeeded; failed-status match; source-agent filter; depth-1
   refusal (a trigger-fired run's completion does NOT re-fire);
   cancelled-via-route finalization fires too (the cancel-route
   finish_paused_run path).
8. **End-to-end + honesty paths**: cron `* * * * *` (1-min) fires a
   mock agent through the worker within ~poll+1min; disable → no new
   runs over 2× period (history intact); catch-up: set next_fire_at
   minutes in the past → exactly ONE new run; delete trigger → runs
   keep trigger_id (renders "deleted trigger"); webhook + event e2e
   smoke re-asserted; two concurrent claim_due_crons never double-fire.
9. **Web**: gen-api regen; TriggersList (table, create → per-kind
   editor, enable toggle + PATCH, delete, fire-now + 409 toast);
   TriggerDetail/editor per kind (cron schedule + next-fire + parse
   errors; webhook URL display + secret-once banner + rotate; event
   status select + optional source agent; input template; lints);
   runs-of-trigger via `?trigger_id=`; ExecutionDetail trigger chip +
   link; capabilities fixtures flip (handlers.ts + TEST_CAPABILITIES);
   msw handlers + trigger fixtures. Gates: tsc, eslint, vitest.
10. **Joint manual session + closure** (post-build, Harshit): draft
   `docs/walkthrough-s13.md` during the build (uncommitted by design);
   the manual session runs it live (a cron trigger on a mock agent
   fired by the scheduler; a webhook POST signed via openssl/python
   streaming like any run; an event chain; disable; the web tour);
   then the closure commit — walkthrough with session notes, README
   triggers section + API rows, roadmap S13 shipped note, CLAUDE.md
   gotchas from the build.

## Rule-7 audit (what touches what)

| Touch | File(s) | Frozen-surface? |
| --- | --- | --- |
| NEW `TriggerRepo` Protocol | `ports/repository.py` | **THE ports/ change #1 — pre-declared ADR 0019 §1** |
| `ExecutionRepo.list_runs` gains `trigger_id` keyword | `ports/repository.py` | **THE ports/ change #2 — pre-declared ADR 0019 §1** |
| `AuthMode` gains `"system"` | `domain/auth.py` | additive Literal member — pre-declared ADR 0019 §2; `is_anonymous` is `mode`'s only branch |
| Trigger domain types | `domain/trigger.py` (new) | new module (domain, listed in ADR §1) |
| migration 0013 | `persistence/migrations/versions/` | new table + column + index |
| `finish_run` optional `on_finalized` + row-rebuilt payload | `persistence/repositories.py` | additive constructor kwarg; finish_run signature UNCHANGED (ADR §5); best-effort, never breaks finalize |
| S14 child/successor metadata provenance copy | `tools/subagents/provider.py` | **pre-declared S14 seam change (ADR §5)**; regression-gated byte-identical when no trigger namespace |
| Scheduler loop + firing service | `runtime/scheduler.py`, `runtime/trigger_firing.py` (new), `api/deps.py`, `api/app.py` lifespan | new modules; worker/queue contract untouched |
| Routes + capabilities + executions filter | `api/routes/triggers.py` (new), `executions.py`, `capabilities.py`, `api/schemas.py` | additive routes (mcp pattern); capabilities detail same commit as migration usage |
| Settings knobs | `config.py` | new `JARVIS_*` fields |
| Web section + provenance chip | `web/src/sections/triggers/*` (new), `ExecutionDetail.tsx`, `routes.tsx` | capability-gated; gen-api regen |
| event envelope / event types / terminal semantics / `RunQueueMessage` / worker claim path / sweeper | — | **frozen** |

## Acceptance (roadmap S13, adapted to this scope)

- A cron trigger fires a mock agent on schedule THROUGH the worker
  (embedded scheduler + embedded worker in one serve process); the
  fired run is an ordinary row in `/v1/executions` with `trigger_id`
  and the `metadata.trigger` namespace.
- A signed webhook POST enqueues and the run streams/replays like any
  run via the existing endpoints; a tampered signature 401s and leaves
  no row; a disabled trigger's webhook is 404 (absent).
- Disabling a trigger stops runs without deleting history; deleting a
  trigger leaves its past runs' provenance intact.
- An event trigger fires when a matching run finalizes (any starter —
  manual, eval, sub-agent child), and a trigger-fired run's own
  completion never re-fires: the depth guard reads the persisted row.
- Exactly-once cron firing across scheduler instances; catch-up after
  downtime fires once, not once per missed slot; enqueue failure is
  an honest `last_error`, never a retry storm.
- Capabilities derives triggers from real rows (a missing migration
  500s nothing — deploy-order tested); the web Triggers section flips
  from capabilities, never hardcoded.
- No triggers exist → agent runs byte-identical (regression-gated);
  the S14 metadata copy is a no-op for provenance-free parents.