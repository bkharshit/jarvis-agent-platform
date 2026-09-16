# ADR 0019 — Triggers (S13): cron schedules, webhooks, and event watchers as queue producers

Status: accepted (planning ADR — no S13 code exists at time of writing)
Date: 2026-09-16
Supersedes: none (implements the roadmap S13 sketch; scope confirmed by
Harshit 2026-09-16 — all three trigger kinds in v1; cron rides croniter +
a Postgres-truth loop, NOT APScheduler)

## Context

Everything shipped so far starts runs only when a human (or a test) calls
`/run`, `/stream`, or an S14 `sub__`/`handoff__` tool. The roadmap S13
sketch asks for runs that start on their own — schedules, inbound
webhooks, and watchers on run outcomes. The sketch's own framing is the
correct one and this ADR keeps it: **a trigger is just a queue producer**
(ADR 0008 §2 pre-declared exactly this — "`enqueue` on the port exists
for future producers (S13 triggers)"). Nothing about the worker, the
event envelope, or terminal semantics changes.

Verified seams this design rides on (paths checked 2026-09-16):

- `runtime/child_runs.py:29-86` — the canonical enqueue-without-a-route
  composition: `child_message` (pre-generated `uuid4` run_id) +
  `enqueue_child_run` (`create_queued_run`, row + message one tx, ADR
  0008 §2). A trigger fires the same shape with no parent.
- `persistence/repositories.py:2081-2108` — the queue's `claim`: a
  subquery with `FOR UPDATE SKIP LOCKED` + one guarded `UPDATE ...
  RETURNING`. This is the ONLY concurrency primitive in the codebase
  (no advisory locks anywhere); the sweeper's exactly-once discipline
  is guarded decision writes (`requeue`/`ack` carry `WHERE status =
  'claimed'`, repositories.py:2021-2123). The cron claim follows this
  model, not a new locking idiom.
- `runtime/worker.py:451-454` — `_sweep_loop` (`while True: sleep(30);
  sweep()`) is the periodic-loop idiom; `deps.py:229-252`
  `start_worker`/`stop_worker` is the container-managed background-task
  pattern (embedded flag + lifespan wiring at `app.py:37-50`).
- `persistence/repositories.py:827-847` — `SqlExecutionRepo.finish_run`
  is the single choke point every terminal path funnels through: the
  runtime tails (`agent_runtime.py:261-263`, `:852-854`;
  `workflow_runtime.py:243-245`, `:315-317`), the worker fail-fast
  paths (`worker.py:189, 213, 256`), the sweeper's terminal handling
  (`worker.py:421` via `_finish_from_terminal`), and
  `finish_paused_run` (`worker.py:578`, shared by the reaper and the
  cancel route). Terminal statuses:
  `TERMINAL_STATUSES = ("succeeded", "failed", "cancelled",
  "timed_out")` (`domain/execution.py:25`).
- `domain/auth.py:20-35` — `Principal` (`tenant_id`, `user_id`,
  `api_key_id`, `role`, `mode: AuthMode`); `AuthMode =
  Literal["anonymous", "session", "api_key"]` (`auth.py:16`). No
  "system" mode exists today (§5 adds one, additive).
- `api/auth.py:79-122` — there is NO auth middleware; auth is opt-in
  per route via the `AuthDep` dependency, so a route that omits it is
  unauthenticated by construction (the `POST /v1/auth/login` and
  `/healthz` precedent). An HMAC-verified webhook ingest route needs no
  new exemption mechanism.
- `src/jarvis/security/crypto.py` — the AES-256-GCM envelope
  `{v, key_id, nonce, ct}` (`encrypt`/`decrypt`, master key by env-var
  name, D18/D30); `api/routes/credentials.py:24-48` is the write-only
  read-model pattern (secret never in GET; missing master key → 503
  `credentials_unavailable`) and `routes/auth.py` API keys are the
  plaintext-exactly-once pattern.
- `domain/workflow.py:275-302` — `render_template(template, values)`,
  `_TEMPLATE_PATTERN = re.compile(r"\{\{([\w.-]+)\}\}")`, unknown vars
  pass through untouched. Reused verbatim for trigger input templates —
  flat values, so the S6 pattern-admission coupling does not apply.
- `api/routes/capabilities.py:53` — the `"triggers"` section flag
  already exists disabled; detail blocks are derived at request time
  from the container (`_evaluations_detail` pattern). The S4 gotcha
  applies: capabilities 500s until migration 0013 is applied — the
  derived-detail branch ships WITH the migration, in one commit.
- `api/routes/_run_routes.py:42-74` — `queue_message` stamps row
  metadata (the workflow `metadata["kind"] = "workflow"` precedent at
  `:57-59`); `ports/queue.py:47-81` `RunQueueMessage` is
  `extra="forbid"` with free-form `metadata` — trigger provenance rides
  it with NO message change.
- Migrations `0001..0012` exist; next is `0013`. Tenant-owned table
  pattern: eval tables (`0011_eval_tables.py` — `tenant_id` NOT NULL,
  FK, `server_default="default"`, index).
- `tools/subagents/provider.py:147-167` — child/successor metadata is
  MINTED fresh (`{"sub_agent": {...}}`, `{"handoff": {...}}`): it does
  NOT copy the parent's metadata namespaces — the loop-hazard §6
  closes.
- `config.py:9-89` — pydantic-settings, `JARVIS_` prefix; the codebase
  is uniformly UTC (`datetime.now(UTC)`; no naive `utcnow()` anywhere
  in src/).

## Decision

### 1. One tenant-owned `triggers` table; three kinds; typed configs (D57)

Migration `0013` creates `triggers` (the eval tenancy pattern —
`tenant_id` NOT NULL FK `server_default="default"` + index):

| column | notes |
| --- | --- |
| `id` | uuid4 PK |
| `tenant_id` | NOT NULL, FK tenants, `"default"` |
| `name` | display string, UNIQUE per tenant (plain composite unique) |
| `kind` | native enum `trigger_kind`: `cron \| webhook \| event` |
| `agent_id` | the agent this trigger FIRES — string id, NO FK (the mcp-name/mcp-binding precedent: loose reference, firing fails honestly with `last_error` if the agent is gone) |
| `agent_version_id` | nullable — the pin for `pinned` policy |
| `version_policy` | `pinned \| latest` |
| `input_template` | nullable text — `{{var}}` substitution, §2 |
| `enabled` | bool, default true |
| `config` | JSONB — kind-typed union (below) |
| `secret` | JSONB nullable — AES-GCM envelope, webhook only, write-only (§4) |
| `next_fire_at` | timestamptz nullable — cron only |
| `last_fired_at` | timestamptz nullable |
| `last_error` | text nullable — the honest miss record (§3) |
| `created_at` / `updated_at` | timestamptz |

The same migration adds `agent_executions.trigger_id` (nullable string,
NO FK + plain index) — the D53 argument: the executions filter needs a
queryable index, not a JSONB scan; and no FK means a trigger's runs keep
their provenance after the trigger is deleted (§7 records the rejection
of the eval-style delete-409-with-runs).

Domain types in NEW `domain/trigger.py` (pydantic, the `domain/mcp.py`
shape): `Trigger` with a discriminated config union —

- `CronConfig{schedule: str}` — a five-field cron expression, validated
  with `croniter` at the API boundary (422 on parse failure). UTC only;
  per-trigger IANA timezones are recorded deferrals (§8).
- `WebhookConfig{}` — no fields v1; the union member exists so the
  discriminated parse stays closed.
- `EventConfig{status: succeeded | failed | cancelled | timed_out | any,
  source_agent_id: str | None}` — the closed condition set v1: "a run of
  this tenant (optionally only `source_agent_id`'s runs) reached this
  terminal status". `any` = every terminal status.

`ports/repository.py` gains a `TriggerRepo` Protocol (the `McpServerRepo`
explicit-`tenant_id`-kwargs pattern — no scoped.py wrapper): `get`,
`list`, `create`, `update`, `delete`, `claim_due_crons` (§3),
`record_fire` (§3), `enabled_event_triggers(tenant_id)` (§6).
`ExecutionRepo.list_runs` gains `trigger_id: str | None = None`
(equality filter — the S14 `parent_run_id` precedent). **These are the
pre-declared rule-7 ports/ changes.** Plus one additive domain change:
`AuthMode` gains `"system"` (§5).

**Version policy** (the roadmap's `pinned|latest`):

- `pinned`: the route stamps `agent_version_id` at create/patch — the
  `_pin_agent_versions` pattern (D42/D51 transposed); 422 when the
  target has no published version. A pinned trigger replays that exact
  version forever (D1); a republished target surfaces as a detail
  **lint** ("target republished since pin"), never a failure.
- `latest`: `agent_version_id` stays NULL on the row; the firing
  service resolves the target's *current published* version at fire
  time. This is the explicit opt-in to drift — no lint, ever.

### 2. Firing composition: a trigger is an ordinary queue producer (D58)

`runtime/trigger_firing.py` — `TriggerFireService.fire(trigger, *,
context_vars) -> run_id`. It composes the child-run shape with no
parent: fresh `uuid4` `run_id`, fresh `trace_id`, `kind="agent"`,
`deadline = deadline_from_now(settings.run_timeout_seconds)` (a fresh
platform budget — there is no parent to inherit), `session_id=None`,
row metadata `{"trigger": {"trigger_id", "kind", "name", "depth"}}`,
then `enqueue_child_run` (row + message one tx). The run is claimed and
executed by the UNCHANGED worker — a trigger-fired run is
indistinguishable from a manual one in `/v1/executions`, exactly like
eval children.

- **The principal is a synthesized service principal**
  `Principal(tenant_id=trigger.tenant_id, mode="system")` — the new
  additive `AuthMode` member. `None` would break stored-credential
  resolution (D28 needs a tenant to resolve BYOK refs), and reusing
  `"api_key"`/`"anonymous"` would be dishonest. `mode`'s only consumer
  is `Principal.is_anonymous` (`mode == "anonymous"`) plus whoami
  serialization — verified, nothing else branches on it.
- **Input**: `render_template(trigger.input_template, values)` —
  `{{trigger_id}}`, `{{trigger_name}}`, `{{now}}` (ISO-8601 UTC)
  everywhere; `{{payload}}` on webhooks (§4); `{{run_id}}`,
  `{{status}}`, `{{agent_id}}`, `{{output}}` (the finished run's
  `final_message`, capped at `trigger_event_max_output_chars`) on event
  triggers. Unknown vars pass through untouched (the render_template
  contract). An EMPTY template gets an honest per-kind default:
  cron → `"Scheduled run of agent <agent_id> by trigger
  '<name>' at <now>."`; webhook → the raw payload itself (capped);
  event → `"Run <run_id> of agent <agent_id> finished with status
  <status>."`.
- **Memory lint**: `session_id=None` means a memory-enabled target
  silently runs memory-less (the S14 child precedent) — a
  publish-time-style detail lint on the trigger ("memory enabled on
  target; trigger-fired runs are memory-less"), the `memory_ignored`
  pattern. Stable per-trigger sessions are a recorded deferral (§8).
- **Depth**: the fired run's `metadata["trigger"]["depth"]` is
  `incoming depth + 1` — 0-born for cron/webhook (a schedule/webhook is
  a root), 1-born for event triggers (§6's guard reads it).

### 3. Cron: croniter + a Postgres-truth scheduler loop; at-most-once (D59)

- **Dependency**: `croniter>=2.0` (cron parsing only — 5-field
  expressions, DST-free under UTC). APScheduler was REJECTED (Harshit's
  scope call, 2026-09-16): it brings its own job-store paradigm we
  would fight to keep tenant/credential-consistent; the roadmap's
  "Postgres-cron loop" option matches the `_sweep_loop` idiom exactly.
- **The loop**: `runtime/scheduler.py` —
  `while True: sleep(trigger_poll_seconds); fire_due()`, with
  `fire_due`'s exceptions contained (log + continue — the loop never
  dies on one bad trigger). `AppContainer` gains
  `start_scheduler`/`stop_scheduler` mirroring `start_worker`
  (`deps.py:229-252`), started in the serve lifespan gated by
  `trigger_scheduler_enabled: bool = True` — independent of
  `embedded_worker` (the scheduler only enqueues; workers anywhere
  execute). The standalone `jarvis worker` CLI does NOT run it (single
  responsibility; the guarded claim tolerates overlap if that ever
  changes).
- **The claim** (`TriggerRepo.claim_due_crons(now, limit)`) — the
  queue-claim model, one transaction:
  `SELECT ... WHERE kind='cron' AND enabled AND next_fire_at <= now
  ORDER BY next_fire_at LIMIT n FOR UPDATE SKIP LOCKED`, then in the
  same tx advance each row (`last_fired_at = now`,
  `next_fire_at = croniter(schedule, now).get_next(datetime)`,
  `last_error = NULL`) and COMMIT. Two scheduler instances racing: the
  row lock makes exactly one claimant win — no advisory locks, the
  codebase stays with its single concurrency idiom.
- **At-most-once, honestly recorded**: the claim commits FIRST, the
  enqueue happens after, per trigger. A crash in between misses the
  fire (the next one is already scheduled) and the enqueue failure path
  writes `last_error` on the row — visible in the UI, never a retry
  storm. This is the opposite trade-off from the queue's claim (a
  missed cron tick is benign; a duplicate side-effect run is not).
  Webhook and event fires are direct-response/direct-context acts —
  their failures surface to the caller (401/500) or the finalize log,
  not to `last_error`.
- **Catch-up policy: fire once, not once-per-missed-slot.** All
  schedule math is `croniter(schedule, now)` — after downtime the
  trigger fires once and continues from now (a `*/5` trigger down for
  an hour fires once at recovery, not twelve times). Creating a cron
  trigger or re-enabling a disabled one stamps
  `next_fire_at = croniter_next(now)`; a disabled trigger's stale
  `next_fire_at` is never honored (the claim requires `enabled`).

### 4. Webhooks: one unauthenticated HMAC route; per-trigger write-only secret (D60)

- **Route**: `POST /v1/triggers/webhook/{trigger_id}` in
  `routes/triggers.py` — takes `ContainerDep` but NOT `AuthDep`
  (no middleware exists; the login precedent). The route NEVER derives
  tenant or principal from the request; the trigger row is the trust
  boundary (the ADR 0008 queue-message stance, transposed to inbound).
- **Signature**: header `X-Jarvis-Signature: sha256=<hexdigest>` —
  HMAC-SHA256 over the RAW body bytes with the trigger's secret;
  constant-time compare (`hmac.compare_digest`). Replay protection
  (timestamp + freshness window) is a recorded deferral (§8).
- **Responses**: missing trigger, non-webhook kind, or disabled →
  **404** (a disabled trigger behaves as absent — the acceptance's
  "disabling stops runs", and the D29 no-existence-leak stance toward
  an unauthenticated caller); wrong/missing signature → **401
  `invalid_signature`**; body over `trigger_webhook_max_body_bytes`
  (default 64 KiB) → **413**; otherwise enqueue (§2 with
  `{{payload}}` = the raw body, UTF-8 `errors="replace"`, capped) and
  **202 `{run_id, status: "queued"}`**. The run streams/replays like
  any run through the EXISTING executions endpoints — the ingest route
  never blocks on the run.
- **The secret** is auto-generated server-side at create
  (`secrets.token_urlsafe(32)`), encrypted with the shared AES-GCM
  envelope (`jarvis.security.encrypt`, master key per D18/D30), stored
  in `triggers.secret`. **Plaintext exactly once**: returned in the
  create response and in `POST /v1/triggers/{id}/rotate-secret`
  (rotation: fresh secret, old invalid immediately) — never in GET,
  never logged, never in snapshots (D29/D30 discipline). A missing
  master key at create/rotate → 503 `credentials_unavailable` (the
  `credentials.py:38-48` pattern), so a webhook trigger always carries
  a secret; the impossible secret-less row verifies as 401.

### 5. Event triggers: a closed condition set, evaluated at the finalize choke point (D61)

- **Conditions v1** (closed set): a terminal-status match, optionally
  scoped to one source agent (`EventConfig`, §1). The sketch's "when a
  tool emits Y" is a recorded deferral (§8) — run outcome is the
  composable, queryable primitive.
- **The evaluation site**: `SqlExecutionRepo.__init__` gains an
  optional `on_finalized: Callable[[Awaitable[None]], ...] | None`;
  `finish_run` invokes it AFTER commit, best-effort
  (`try/except` + log — a trigger side-effect can never break a
  terminal write; rule 2). ONE seam catches every finalize path by
  construction: runtime tails, worker fail-fast, the sweeper, the
  pause-reaper, the cancel route, even CLI-run finals. The container
  wires the callback to the trigger service (`deps.py`) — the repo
  stays persistence-shaped, the composition lives where it always does.
- **The row is the truth — a load-bearing detail**: `finish_run`
  passes the observer the result reconstructed from the ROW after the
  write (status AND `row.metadata`), not the caller's `RunResult`. The
  runtime's in-memory result can disagree with the row's metadata (the
  worker finalize path constructs results independently), and the
  observer's loop guard (below) must read the persisted provenance —
  trusting the passed-in metadata would let a crafted/absent value
  bypass the guard.
- **The guard — depth, by construction**: `TriggerService.on_run_finalized`
  skips when the finalized run's own
  `metadata["trigger"]["depth"] >= trigger_max_depth` (default 1: a
  manual run's outcome may fire triggers; a trigger-fired run's outcome
  may not fire another). The fired run carries `depth = incoming + 1`
  (§2), so the chain is clamped at the checkpoint like every other
  ADR-0004 limit.
- **The child/successor copy — a pre-declared S14 seam change**: S14's
  child/successor metadata is minted fresh
  (`tools/subagents/provider.py:147-167`) and does NOT inherit the
  parent's `trigger` namespace. Without a copy, an event trigger on a
  child-agent's completion re-fires through every generation
  (fire → parent delegates → child completes with depth 0 → fire → …)
  — the guard is bypassed at each hop. Fix: both S14 producers
  (sub-agent child, handoff successor) copy the parent's
  `metadata["trigger"]` verbatim when present. Regression-gated: a
  parent without trigger provenance composes byte-identical children.
- Matching runs fire per §2 with the event context vars; failures log
  and stamp `last_error` on the trigger row — the finalizing run is
  NEVER affected. Eval children, sub-agent children, and handoff
  successors are ordinary runs and DO fire event triggers (whoever
  started them) — honest and composable, bounded only by the depth
  guard.

### 6. Provenance, API surface, capabilities, UI (D62)

- **Row + metadata**: every fired run carries the real
  `trigger_id` column (filterable, indexed) AND the
  `metadata["trigger"]` namespace (kind, name, depth — the
  workflow-kind/eval/sub_agent stamping style; the sketch's flat
  `metadata.trigger_id` is namespaced for consistency with every other
  metadata producer).
- **Routes** (`/v1/triggers`, the mcp CRUD pattern — `AuthDep` +
  explicit-tenant repo calls, owner/admin-manage + member-read, foreign
  tenant 404 D29):
  - `GET /v1/triggers` (list, per-tenant), `GET /v1/triggers/{id}`
    (detail: computed `next_fire_at`, `last_fired_at`, `last_error`,
    lints — stale pin, memory-on-target; NO secret field ever).
  - `POST /v1/triggers` (create — validates the kind's config,
    stamps the pin for `pinned` policy, generates + returns-once the
    webhook secret; 422 bad cron expression / no published version /
    unparseable config; 403 anonymous-manage in required mode — the
    mcp role-gate pattern).
  - `PATCH /v1/triggers/{id}` (FULL-document PATCH — the
    established mcp/eval discipline); enabling a cron trigger
    recomputes `next_fire_at` from now; disabling clears it.
  - `DELETE /v1/triggers/{id}` — 204, ALWAYS allowed (no-FK provenance:
    past runs keep their `trigger_id` and render "deleted trigger
    <id>" honestly). The eval-style 409-with-runs was REJECTED — the
    acceptance only demands disable-stops-runs, and permanent
    undeletable rows are the worse failure.
  - `POST /v1/triggers/{id}/rotate-secret` (webhook only; new secret
    returned once).
  - `POST /v1/triggers/{id}/fire` — manual test-fire (the same §2
    composition with cron/webhook defaults); **409 when disabled** —
    "disabled fires nothing" stays a true sentence.
  - `GET /v1/executions?trigger_id=` — the new filter (the S14
    parent_run_id route pattern).
- **Capabilities**: `triggers.enabled: true` with derived detail
  (tenant trigger counts by kind — the `_evaluations_detail` pattern).
  The branch ships in the SAME commit as migration 0013's usage (the S4
  500s-the-whole-shell gotcha); the web msw capabilities fixture flips
  with it.
- **UI** (the roadmap's enablement line): the Triggers section flips
  on (registry + placeholder route already exist) — list + per-kind
  editor (cron schedule + next-fire display + honest parse errors;
  webhook URL `POST /v1/triggers/webhook/{id}` + secret shown once +
  rotate; event status + optional source agent), enable/disable toggle,
  fire-now button, "runs of this trigger" via `?trigger_id=`;
  ExecutionDetail gains a trigger-provenance chip + link on fired runs.

## Deliberately out of v1 (recorded, not silently dropped)

- **Per-trigger IANA timezones** — cron is UTC-only (the codebase is
  uniformly UTC; a `timezone` config field is the upgrade path).
- **Webhook replay protection** — no timestamp/freshness window; HMAC
  over the body only. A `X-Jarvis-Timestamp` + window check slots into
  the same verifier.
- **The "tool emits Y" event condition** — run outcome is the v1
  primitive; a tool-call condition engine rides the same observer seam.
- **Stable per-trigger sessions** (a cron agent that remembers its last
  run) — `session_id=None` + the memory lint is the S14-child-consistent
  v1; a `session_id = f"trigger:{id}"` scheme must first solve
  overlapping fired runs interleaving one conversation.
- **Pause notifications** (implementation-plan-s10's "pause webhooks")
  — an `EventConfig` extension (`status: awaiting_input`) once its
  non-terminal semantics at the observer seam are designed; today the
  observer only sees terminal states (`finish_run`).
- **Backpressure / concurrency caps per trigger** — a burst webhook
  enqueues one run per POST, bounded only by the platform's ordinary
  means; per-trigger rate limits are future hardening (S9-shaped).
- **Trigger versioning / audit** — triggers are mutable rows (no
  snapshot discipline); the fired runs + their pinned version are the
  audit trail.

## What stays frozen

- The event envelope, all event types, terminal semantics — unchanged;
  a trigger-fired run is an ordinary run end to end.
- `RunQueueMessage` — unchanged (provenance rides the free-form
  `metadata`); the worker claim path — unchanged (no new `kind`).
- The queue's claim/lease/sweep/ack contract and the sweeper — the
  scheduler is a new producer + a new loop, zero queue changes.
- The error envelope; tenancy rules (D29) — trigger rows are
  tenant-owned, foreign ids 404, the webhook ingest leaks nothing.
- Secret rules (ADR 0005/0006, D29/D30) — the webhook secret reuses the
  AES-GCM envelope and master-key discipline; never returned by GET,
  never logged.
- `ports/` — the ONLY changes are the pre-declared new `TriggerRepo`
  Protocol, the additive `list_runs` keyword, and (domain, listed for
  the same reason as ADR 0018's domain set) the `AuthMode "system"`
  member. Everything else rides existing Protocols.

## Consequences

- One new producer surface to audit: the firing service composes the
  child-run shape; every fired run is visible, cancellable, and
  replayable like any run — triggers add no new execution semantics.
- Exactly-once cron firing across scheduler instances rides the
  codebase's only concurrency idiom (SKIP LOCKED + guarded writes);
  at-most-once per fire (claim commits before enqueue) with `last_error`
  as the honest miss record.
- The event-trigger observer is a post-commit hook on the single
  terminal choke point — future finalize paths (any new terminal
  writer) inherit trigger evaluation for free, and the guard reads the
  ROW's metadata, so it cannot be bypassed by in-memory results.
- The S14 metadata copy (§5) is the one touch on shipped S14 code —
  small, pre-declared, regression-gated byte-identical for
  provenance-free parents.
- Deploy order matters once: migration 0013 before the capabilities
  branch (the S4 lesson, now recorded in the plan's commit 5).
- `croniter` joins the dependency list — the first scheduling lib; the
  loop around it stays ours (Postgres truth, one idiom).