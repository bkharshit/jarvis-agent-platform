# S1 manual walkthrough — distributed runs (ADR 0008), verified live

A hands-on reproduction of the S1 acceptance tests against a live stack,
with the "why" and the concept behind each step. Verified 2026-09-05
against dev DB `jarvis`, backend on :8001, agent `gemma-cloud-agent`.

One rule to keep in mind while reading (D26): **queue-always, no dual
mode**. The API never executes runs — it writes a message to `run_queue`
and returns. A worker claims messages and executes runs; everyone else
tails the DB by cursor. `JARVIS_EMBEDDED_WORKER=true` (the default) just
means "also start the worker inside the API process" — the distributed
code path runs on *every* run.

New schema (see `psql ... -c "\dt"`): `run_queue` (messages, claims,
leases) and `run_cancels` (durable cancel requests), alongside
`agent_executions` / `execution_events`.

## 0. Setup

```bash
lsof -nP -iTCP:8001 -sTCP:LISTEN      # kill any stale backend first
uv run alembic upgrade head           # migration 0003: run_queue, run_cancels
uv run jarvis serve                   # default mode: API + embedded worker
```

`queued` windows are sub-second with the embedded worker — to *see* the
new status you need a worker-less interval (tests 4 and 8 below).

## 1. Baseline — default mode is unchanged

```bash
curl -s -X POST localhost:8001/v1/agents/<agent-id>/run \
  -H 'Content-Type: application/json' \
  -d '{"input":"Say hello in exactly 5 words."}'
# → "status": "succeeded"
```

**Proves** the embedded-worker default behaves exactly like Phase 1 from
the outside — while internally going enqueue → claim → execute → append.

**Concept:** invisible distribution. The queue is exercised on every run,
so the distributed path can't rot.

## 2. The event stream

```bash
curl -s -N -X POST localhost:8001/v1/agents/<agent-id>/stream \
  -H 'Content-Type: application/json' -d '{"input":"Count from 1 to 10."}' \
  > /tmp/s1-stream.log
grep -oE '^event: [a-z_.]+' /tmp/s1-stream.log | tail -1   # → run.completed
grep '^id: ' /tmp/s1-stream.log | tail -1                  # → id: <cursor>
```

**Proves** events flow worker → DB → subscriber, each frame carrying its
cursor as `id:`.

**Concept:** `PgEventStream` (ADR 0008 §4). The worker persists events and
fires a Postgres `NOTIFY`; subscribers *tail the DB by cursor* and use
NOTIFY only as a wake-up. A missed notify costs the 1s fallback poll, never
correctness. The cursor is the `Last-Event-ID` contract from ADR 0003.

## 3. The queue is real — run with no worker

```bash
pkill -f "jarvis serve"        # kill even the embedded worker
uv run jarvis serve &          # restart API-only:
JARVIS_EMBEDDED_WORKER=false uv run jarvis serve &
curl -s -X POST .../run -d '{"input":"x"}' > /tmp/out.log &   # client waits
psql ... -t -c "SELECT e.id, e.status, q.status AS queue_status
                FROM agent_executions e JOIN run_queue q ON q.run_id = e.id
                ORDER BY e.created_at DESC LIMIT 1"
# → queued | pending
uv run jarvis worker &         # now start a worker
# run flips queued → running → succeeded; the waiting client gets its result
```

**Proves** producer/consumer decoupling: the run waits honestly in
Postgres, any worker can claim it, and a client blocked on `/run` is
subscribed to a stream — not to a process.

**Concept:** `queued` is a first-class `ExecutionStatus` (the web UI got a
badge/filter for it). `create_queued_run` writes the execution row and the
queue message in **one transaction**, so a message can never dangle
without its run.

## 4. Kill the API mid-run — the headline

```bash
JARVIS_EMBEDDED_WORKER=false uv run jarvis serve &
uv run jarvis worker &
curl -s -N -X POST .../stream -d '{"input":"Write a 100-word story."}' &
# wait for the run to be running, then:
pkill -f "jarvis serve"        # API dies mid-run
# worker keeps going; restart the API later and check:
curl -s localhost:8001/v1/executions/<run-id> | jq '.run.status, .run.finished_at'
# → "succeeded", finished several seconds AFTER the API died
```

**Proves** the failure S1 exists for: the run outlives the process that
started it.

**Concept:** the run never lived in the API. The worker executes; events
land in the DB; the API, when it returns, just reads the truth.

## 5. Resume at the terminal cursor (bug-fix: stream end)

```bash
LAST=$(grep '^id: ' /tmp/s1-stream.log | tail -1 | cut -d' ' -f2)
curl -s -N -X POST .../stream \
  -H "Last-Event-ID: $LAST" -H 'Content-Type: application/json' \
  -d '{"input":"x","run_id":"<run-id>"}'
# → empty response, returns immediately (used to hang forever)
```

**Proves** reconnecting after consuming the terminal event ends the stream
at once — *nothing missed is a valid answer*.

**Concept:** empty replay + terminal run row ⇒ stream ends. The check
reads the run row via an injected `run_status_fn` (routes always create
the row before subscribing, so an unknown row keeps waiting).

## 6. Resume mid-run — exactly-once delivery

```bash
EARLY=$(grep '^id: ' /tmp/s1-stream.log | sed -n '5p' | cut -d' ' -f2)
curl -s -N -X POST .../stream \
  -H "Last-Event-ID: $EARLY" -H 'Content-Type: application/json' \
  -d '{"input":"x","run_id":"<run-id>"}' > /tmp/resume.log
grep -c '^id: ' /tmp/resume.log      # = total − 5
grep '^id: ' /tmp/resume.log | head -1   # = original frame 6 (no duplicate)
```

**Proves** no gaps, no duplicates across a disconnect: cursors from the
resumed stream join the original prefix contiguously.

**Concept:** the cursor is the resumability contract; delivery
correctness comes from cursor arithmetic, not from NOTIFY.

## 7. Cancel a queued run — durable cancel requests

```bash
# no worker anywhere:
curl -s -X POST .../run -d '{"input":"long task"}' > /tmp/cancel.log &
psql ... -c "SELECT * FROM run_cancels ORDER BY created_at DESC LIMIT 1"
# after: POST /v1/executions/<run-id>/cancel  → {"cancelled": true, "status": "queued"}
#        run_cancels row: "cancelled by user"
uv run jarvis worker &    # worker comes up LATER
# → run status: cancelled; events: exactly [run.cancelled]; model never invoked
```

**Proves** a queued run can be cancelled before any worker sees it, and
the request survives process death.

**Concept:** cancel is a durable request, not a signal (ADR 0008 §6).
The worker pops `run_cancels` in its heartbeat and *before claiming* — a
pre-cancelled run fast-fails without ever starting the runtime.
Cancellation is never failure (D5): terminal is `run.cancelled`.

## 8. Worker crash — the sweeper

```bash
uv run jarvis worker &                 # worker A
curl -s -N -X POST .../stream -d '{"input":"<long generation>"}' &
# the instant the run row says "running":
kill -9 $(pgrep -f "bin/jarvis worker")    # hard crash, mid-run
psql ... -c "SELECT status FROM agent_executions ..."   # stuck at "running"
uv run jarvis worker &                 # worker B (has a live sweeper)
sleep 55                                # 15s lease + 30s sweep interval
psql ... -c "SELECT status, error, error_kind FROM agent_executions ..."
# → failed | worker lost (lease expired) — run did not finish within its claim | timeout
```

Event tail for the run: `… model.invocation.started → run.failed` —
exactly one terminal, appended by the sweeper; the SSE client receives it.

**Proves** recovery from a worker crash *while executing*: no zombie
`running` rows, no duplicate terminals.

**Concept:** leases + reaper (D27). A claim is a 15s lease, renewed by
heartbeat every 2s; the sweeper ticks every 30s and decides by evidence:
zero events ⇒ requeue; any events ⇒ the executor is gone, terminate
honestly with exactly one `run.failed` (`error_kind: timeout`). A worker
that fails to renew *its own* lease cancels its own run first, so no run
is ever double-executed.

## Notes for reproduction

- gemma finishes short generations in seconds; command round-trips are
  slower — for the sweeper test, poll the row in a loop and kill the
  worker in the same shell command the moment the row flips to `running`.
- After killing the API, the SSE client's last `id:` may already be the
  terminal cursor (the run finished meanwhile) — that is test 5, not a
  failure.
- `pkill -f "jarvis serve"` also stops the embedded worker; the standalone
  `jarvis worker` is a separate process and survives it (that's the point).
- Restore normal state: `uv run jarvis serve` (embedded worker on by
  default).

## Where this lives in the code

| Piece | File |
| --- | --- |
| Queue port + messages | `src/jarvis/ports/queue.py` |
| Postgres queue, leases, sweeper SQL | `src/jarvis/persistence/repositories.py` (`SqlRunQueue`) |
| Worker loop, heartbeat, sweep | `src/jarvis/runtime/worker.py` |
| DB-tail event stream / notifier | `src/jarvis/events/pg_notify.py` |
| Queue-backed API routes | `src/jarvis/api/routes/agents.py`, `executions.py` |
| Embedded worker wiring | `src/jarvis/api/app.py`, `deps.py`, `config.py` |
| CLI entrypoint | `src/jarvis/cli/main.py` (`jarvis worker`) |
| Decisions | `docs/decisions.md` D26/D27, `docs/adr/0008-distributed-runs.md` |