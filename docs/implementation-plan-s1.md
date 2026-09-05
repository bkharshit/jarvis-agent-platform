# JARVIS — Implementation Plan (Stage S1: distributed runs & queue-backed events)

- **Status**: In progress
- **Date**: 2026-09-05
- **Rides on**: ADR 0003 (`EventSink`/`EventStream` ports, global cursor),
  ADR 0004 (orchestrator owns limits), D2/D3 (stream resume prefers the
  live sink, RUNNING row at run start), D5 (a run never raises), D6
  (cooperative cancellation), roadmap §S1, **ADR 0008** (this stage's
  architecture).

## Context

Phase 1 runs execute inside the API process; killing `jarvis serve` kills
in-flight runs, and the event bus is one process's memory. S1 moves every
run onto a durable queue with workers, fans events out through Postgres
`LISTEN/NOTIFY`, and makes cancellation cross-process — **without changing
the API surface, the event envelope, or cursor semantics**. The F1 UI
(run console, executions, replay) must work *unchanged* against the
distributed sink; its existing behavior is the acceptance test.

Verified seams this stage rides on (read in code, not from docs):

- `ports/events.py` — `EventSink` (append/finalize, per-run gapless
  sequence) and `EventStream` (replay + subscribe). No consumers of
  `EventStream` yet; routes use the sink's pair-yielding `subscribe` for
  local runs and `SqlExecutionRepo.replay_with_cursor` for foreign ones.
- `events/bus.py` — `InProcessEventSink` takes a `persist` callback whose
  return value is the durable global cursor. The worker reuses this sink
  unchanged with a persist callback that also NOTIFYs.
- `persistence/models.py` — `execution_events.cursor` is a global
  BIGSERIAL (multi-writer ready); `agent_executions.status` is a native
  enum `execution_status` (0001 gotcha: never `create_type` by hand).
- `runtime/agent_runtime.py` — `run()` writes the RUNNING row itself via
  `create_run` and never raises (D5); cancellation is a token checked at
  `check_limits()` (D6).
- `api/routes/agents.py` — `/run` blocks on `runtime.run()`; `/stream`
  subscribes to a sink created *before* the run task so no event is missed;
  resume = `Last-Event-ID` (+ `run_id` body), live sink wins, else DB
  replay.
- `tests/integration/conftest.py` — the `app` fixture does **not** run the
  lifespan (ASGITransport) and hands the container to `app.state` directly;
  the embedded worker therefore must be startable/stopable on
  `AppContainer` itself and the conftest gains one start/stop pair.

## Confirmed decisions

1. **Queue always, no dual run-mode.** There is no `inline` API mode to
   maintain: the queue is the only execution path, so the distributed sink
   is the tested one (ADR 0008 §1). In-process execution remains exactly
   where it belongs: unit tests calling `runtime.run()` directly, the CLI
   `run --stream`, and the embedded worker (same process, still via the
   queue).
2. **Embedded worker by default.** `JARVIS_EMBEDDED_WORKER` (default
   `true`) keeps `jarvis serve` a working standalone for dev and the web
   e2e; distributed deployments turn it off and run `jarvis worker`
   separately. `JARVIS_WORKER_CONCURRENCY` (default 4) caps parallel runs
   per worker. Lease/renew/sweep cadences are module constants, not
   settings knobs (renew 2s, lease 15s, sweep 30s) — tunable later if a
   need appears.
3. **Postgres is the only infrastructure added** (roadmap: "zero new
   infra"): queue table + `SKIP LOCKED`, `LISTEN/NOTIFY` for wake-ups.
   Redis adapters are later swaps behind `RunQueue`/`EventStream`.
4. **Delivery semantics: exactly-once from the DB.** NOTIFY is only a
   wake-up; subscribers tail `execution_events` by global cursor and
   de-duplicate by "last yielded cursor", so missed NOTIFYs cost latency,
   never correctness. Terminal detection is from the terminal event itself
   plus the run row (crash-between-finalize-and-finish covered by the
   sweeper).
5. **Contract amendment (ADR 0008 §5):** `EventStream.subscribe` yields
   `(cursor, event)` pairs — every consumer needs the durable cursor for
   `Last-Event-ID`; the in-process sink already used this shape. Cursor
   semantics themselves are unchanged (ADR 0003).
6. **Leases, not at-most-once.** Expired lease + 0 persisted events →
   requeue (nothing was emitted; re-execution from scratch is consistent).
   Expired lease + any events → exactly one terminal `run.failed`
   appended at `max(sequence)+1` (blind re-execution would collide with
   UNIQUE(execution_id, sequence) and duplicate events). A worker whose
   own renewal fails cancels itself (assume someone else may have acted).

## Design

### Data model (migration 0002 + 0003)

- `0002_queued_status`: `ALTER TYPE execution_status ADD VALUE IF NOT
  EXISTS 'queued'` — must run in an **autocommit block** (PG cannot add
  enum values inside a transaction; distinct from 0001's create_type
  gotcha, same family). Domain `ExecutionStatus` literal + repo tuple gain
  `"queued"` (first position — append-only ordering by created_at keeps
  list views stable).
- `0003_run_queue`:
  - `run_queue` — `id BIGSERIAL pk`, `run_id` (unique, = execution id),
    `payload JSONB` (the `RunQueueMessage`), `status` native enum
    (`pending | claimed | done`), `claimed_by`, `claimed_at`,
    `lease_until`, `created_at`.
  - `run_cancels` — `run_id pk`, `reason`, `created_at` (a *request* row;
    the worker deletes it after triggering its token).

### Ports

- `ports/queue.py` (new): `RunQueueMessage` (pydantic: run_id, agent_id,
  agent_version_id, input, session_id, user_id, trace_id, metadata,
  variables, deadline — an absolute datetime computed at enqueue so the
  deadline survives cross-process —, enqueued_at) + `RunQueue` Protocol:
  `enqueue / claim(worker_id, lease) / ack / renew / sweep`.
- `ports/events.py`: `EventStream.subscribe` yield type becomes
  `tuple[int, ExecutionEvent]` (ADR 0008 §5). `EventSink` untouched.

### Adapters

- `SqlRunQueue` — `claim`: single statement
  `UPDATE run_queue SET status='claimed', claimed_by=…, lease_until=now()+lease
   WHERE id = (SELECT id FROM run_queue WHERE status='pending'
               ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING *`
  (one round-trip, no lock contention between workers); `ack` → `done`;
  `renew` → extend `lease_until` only when still claimed by *this* worker
  (returns False otherwise); `sweep` helpers for expired claims.
- `SqlExecutionRepo` gains: `create_queued_run(run, message)` — the
  execution row (`status='queued'`) + queue message in **one transaction**
  (ADR 0008 §2); `mark_running(run_id, started_at)` — worker-side flip of
  the queued row (runtime's `create_run` insert stays for in-process
  callers: CLI/tests); `next_event_sequence(run_id)` +
  terminal-append helper for the sweeper's exactly-one-terminal rule;
  `latest_event(run_id)` for the crash-after-finalize sweep.
- `PgNotifier` + `PgEventStream` (`events/pg_notify.py`) — one dedicated
  asyncpg LISTEN connection (pooled SQLAlchemy connections cannot hold a
  LISTEN) with reconnect; `PgEventStream implements EventStream`:
  `subscribe(run_id, last_cursor)` = loop { `replay_with_cursor` since
  last yielded cursor → yield pairs → stop on terminal event → else await
  NOTIFY wake (with a periodic DB-poll fallback timeout, so a missed
  NOTIFY only delays, never drops) }; `replay` = DB only. The worker's
  persist callback: `append_event` (commit) then `pg_notify` with
  `{run_id, cursor}`.

### Worker (`runtime/worker.py` + CLI `jarvis worker`)

Depends on ports (`RunQueue`, repos, runtime) so unit tests use in-memory
fakes. Loop:

- **Claim** up to `worker_concurrency` messages (semaphore-bounded tasks).
- **Execute**: load `AgentVersion` by id (new `SqlAgentRepo.get_version_by_id`),
  build `ExecutionContext` from the message (deadline from the message;
  fresh `CancellationToken`), `mark_running`, `runtime.run(version, input,
  ctx, sink=worker_sink)` — then `ack`. A pending cancel row checked
  *before* executing → immediate `RunCancelled` terminal, no work.
- **Heartbeat per live run** (every 2s): `renew` the lease; if renew says
  False → trigger own token ("worker lost lease"); poll `run_cancels` for
  this run → trigger token + delete row. This is the cross-process cancel
  path; cancellation latency ≤ 2s + the next checkpoint (D6 semantics
  unchanged).
- **Sweep** (every 30s): expired-lease claimed messages → requeue if the
  run has 0 events, else append terminal `run.failed` at `max(sequence)+1`
  + `finish_run(failed, "worker lost (lease expired)")` + mark message
  done; RUNNING runs whose latest event is terminal → `finish_run` from
  that event (crash between `finalize()` and `finish_run()`).

### API rewiring (routes only; surface unchanged)

- `POST /agents/{id}/run` — resolve definition+version → build ctx (as
  today) → `create_queued_run` → drain `PgEventStream.subscribe` until the
  terminal event → return the final `RunResult` from the repo. Still a
  blocking run; it is simply *waited for* instead of *executed here*.
- `POST /agents/{id}/stream` — same enqueue, then frame
  `PgEventStream.subscribe` events. Resume (`Last-Event-ID` + `run_id`)
  goes through `PgEventStream` in all cases — the in-process sink path is
  removed from routes because with the queue the *worker* owns the run and
  a local sink could silently never fire (embedded + external workers
  compete for claims). NOTIFY is the live bus now; the local
  `InProcessEventBus` keeps serving the runtime's default, CLI, and tests.
- `GET /executions/{id}/events` (SSE) — same `PgEventStream` subscription.
- `POST /executions/{id}/cancel` — `runtime.cancel()` first (in-process
  embedded worker: instant); if not triggered, write `run_cancels`
  (`ON CONFLICT DO NOTHING` — idempotent). Cancel result stays
  `CancelResult(run_id, cancelled, status)`; status is still `running` —
  cancellation remains cooperative.
- Lifespan: when `settings.embedded_worker`, start the worker on the
  container and stop it on shutdown. Integration conftest does the same on
  its container fixture (no lifespan under ASGITransport).

### Web (UI enablement: none — behavior must be unchanged)

- Regenerate the API client (`make gen-api`): `ExecutionStatus` gains
  `"queued"`.
- `ExecutionsList`: status filter list + a `queued` badge color; the run
  console needs no change (a queued run simply shows connecting until the
  first event arrives).

## Repository layout (end state of S1)

```
src/jarvis/
  ports/queue.py                # RunQueueMessage + RunQueue Protocol   (new)
  events/pg_notify.py           # PgNotifier + PgEventStream             (new)
  runtime/worker.py             # claim/execute/heartbeat/sweep          (new)
  persistence/models.py         # RunQueueRow, RunCancelRow              (mod)
  persistence/repositories.py   # SqlRunQueue, create_queued_run, …      (mod)
  persistence/migrations/versions/0002_queued_status.py, 0003_run_queue.py
  api/routes/{agents,executions}.py                             (mod)
  api/deps.py                   # worker lifecycle on the container      (mod)
  cli/main.py                   # jarvis worker                          (mod)
  config.py                     # embedded_worker, worker_concurrency    (mod)
  domain/execution.py           # ExecutionStatus + "queued"             (mod)
web/src/sections/executions/ExecutionsList.tsx                  (mod)
web/openapi.json, web/src/api/schema.d.ts                      (regen)
```

## Commit sequence (each independently green)

| # | Commit | Gates |
|---|--------|-------|
| 0 | `docs(adr): ADR 0008 + S1 implementation plan` | docs only |
| 1 | `feat(domain): queued execution status + migration 0002` | unit + `pytest -m db tests/integration/test_migrations.py` |
| 2 | `feat(ports): RunQueue port + queue message` | unit |
| 3 | `feat(persistence): run_queue/run_cancels tables + adapters (migration 0003)` | unit + integration repos tests |
| 4 | `feat(events): LISTEN/NOTIFY PgEventStream` | unit + integration |
| 5 | `feat(runtime): worker loop + jarvis worker` | unit (fakes) |
| 6 | `feat(api): queue-backed run/stream/cancel + embedded worker` | unit + **full integration suite** (acceptance: existing suite passes unchanged) |
| 7 | `feat(web): queued status in executions views` | tsc, eslint, vitest |
| 8 | `docs(s1): decisions D25/D26, README runbook, overview interfaces` | docs only |

Every commit: `pytest tests/unit`, `ruff check src tests`, `mypy src`
green before committing; integration suite green before commits 6; web
gates green on commit 7.

## Risks & deliberate deferrals

- **`ALTER TYPE … ADD VALUE` in alembic** — autocommit block required;
  test the migration itself (integration) not just the ORM enum.
- **NOTIFY payload ≤ 8000 bytes** — payload is `{run_id, cursor}` only.
- **False reaping on a DB hiccup > 15s** — renewal failure makes the
  worker cancel *itself*, so a false positive ends the run cleanly rather
  than double-executing; the sweeper never re-executes.
- **A `/run` with no worker running waits forever** — accepted for S1
  (embedded worker is on by default; distributed deployments run workers);
  a claim-wait timeout is a small follow-up if it ever bites.
- **Deferred**: mid-run resume after worker death (needs event-sequence
  continuation — sweeper fails such runs instead); Redis queue/stream
  adapters (seams exist, zero callers need them yet); S13 triggers as
  queue producers.

## Verification (S1 exit criteria)

1. `pytest tests/unit`, `ruff check src tests`, `mypy src` green.
2. **Full existing integration suite passes unchanged** (SSE
   exactly-once, terminal semantics, replay ≡ live) — now through the
   queue-backed sink.
3. Acceptance (roadmap §S1), automated where possible + one manual run:
   backend with `JARVIS_EMBEDDED_WORKER=false` + `uv run jarvis worker`;
   start a streaming run with a slow tool; kill `serve` mid-run → worker
   finishes; an SSE client reconnecting with `Last-Event-ID` observes no
   gaps and no duplicates.
4. Web: `tsc --noEmit`, eslint, vitest, and the Playwright e2e smoke
   green against the queue-backed backend (run console works unchanged).