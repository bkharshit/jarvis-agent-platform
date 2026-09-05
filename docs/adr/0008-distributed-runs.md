# ADR 0008 — Distributed runs: queue-backed execution, NOTIFY event fan-out, cross-process cancel

- **Status**: Accepted
- **Date**: 2026-09-05

## Context

Phase 1 executed every run inside the API process (ADR 0003 kept the
`EventSink`/`EventStream` ports deliberately forward-looking: "anticipates
swapping this for a queue-backed implementation without touching callers").
Roadmap S1 makes runs survive the API process: a run started against one
process must finish in a worker, and its events must stream to subscribers
of *other* processes — without changing the API surface, the event
envelope, or cursor semantics.

Two structural facts from Phase 1 carry over: the global
`execution_events.cursor` BIGSERIAL already assumed multiple writers, and
`Last-Event-ID` resume already only ever read from the DB.

## Decision

1. **Every run goes through a durable queue.** `POST /run|/stream` writes
   the execution row (`status: queued`) *and* a queue message in **one
   transaction**, then returns/subscribes; a worker claims the message and
   executes the run through the unchanged `AgentRuntime`. There is no
   in-process execution mode in the API — the queue is the only path, so
   the distributed sink is also the default one (the F1 run console and
   executions views are the acceptance test, unchanged).
2. **`RunQueue` is a new port** (`ports/queue.py`): `enqueue / claim / ack
   / renew / sweep`. The Phase 1 adapter is Postgres
   (`SELECT … FOR UPDATE SKIP LOCKED` + lease column); a Redis adapter can
   replace it later behind the same protocol. The API's atomic
   row+message insert lives in `SqlExecutionRepo.create_queued_run` (both
   tables belong to the persistence layer) — `enqueue` on the port exists
   for future producers (S13 triggers).
3. **Worker leases, never at-most-once hopes.** A claim holds a lease
   (`lease_until`); the worker renews it on a heartbeat that *also* polls
   the cross-process cancel table. A failed renewal cancels the worker's
   own run (a lost lease means another worker may act — continuing would
   double-emit events). The sweeper reaps expired leases: a run with **no
   persisted events is requeued** (safe — nothing was emitted); a run with
   events gets exactly one terminal `run.failed` appended at
   `max(sequence)+1` (never re-executed — the per-run gapless sequence and
   UNIQUE(execution_id, sequence) make blind re-execution impossible).
4. **EventStream fan-out = DB tail woken by LISTEN/NOTIFY.** The worker's
   sink persists events (unchanged `append_event` commit) then
   `pg_notify('jarvis_events', {run_id, cursor})`. Subscribers tail
   `execution_events` by global cursor and use NOTIFY only as a wake-up —
   the DB is the sole source of truth, so delivery stays exactly-once even
   when a NOTIFY is missed. Postgres NOTIFY is the Phase 1 "live bus";
   Redis Streams remain a later swap behind the same port.
5. **`EventStream.subscribe` yields `(cursor, event)` pairs.** The Phase 1
   port typed `subscribe` as yielding bare events, but every real consumer
   (the SSE `Last-Event-ID`) needs the durable global cursor, and the
   in-process sink already yielded pairs. This aligns the port with the de
   facto shape; cursor *semantics* (ADR 0003) are unchanged. `replay`
   stays events-only.
6. **Cancellation is cross-process and stays cooperative.** `POST
   /executions/{id}/cancel` first triggers the in-process token (covers
   the embedded worker — instant), else writes a `run_cancels` row; the
   owning worker's heartbeat sees it and triggers the same token at the
   next `check_limits()` checkpoint (D6 unchanged — a sleeping tool still
   finishes).

## Consequences

- `jarvis serve` runs an **embedded worker by default** (`JARVIS_EMBEDDED_WORKER`,
  default on): a single process behaves exactly like Phase 1 from the
  outside, but through the queue. Distributed deployments set it to `false`
  and run `jarvis worker` separately — that is also the mode in which the
  kill-the-API-mid-run acceptance holds.
- The run console, executions views, replay, and conversations need no
  changes beyond rendering the new `queued` status.
- New failure modes to test: lost leases, missed NOTIFYs, crash between
  `finalize()` and `finish_run()` (sweeper finishes such runs from their
  terminal event).
- A run still *executing* when its API process dies is not lost — it was
  never in that process. A run whose **worker** dies mid-flight is failed
  by the sweeper with a terminal event (mid-run event-sequence resume is
  deliberately deferred).

## Alternatives Considered & Trade-offs (Why Not Kafka or RabbitMQ?)

1. **Dual-Write Hazard (Kafka/RabbitMQ vs PostgreSQL Queue)**:
   Publishing an execution to an external broker alongside inserting the `agent_executions` DB row requires two-phase commits or the Transactional Outbox Pattern to prevent dangling records. With `SqlExecutionRepo.create_queued_run`, the execution row (`status='queued'`) and the `run_queue` message are committed in **one single ACID transaction**.
2. **Task Lifecycle Mismatch (Kafka Partitioning)**:
   Kafka is an append-only event streaming platform, not a task queue. Variable 10s–60s LLM execution durations risk tripping consumer group rebalance timeouts (`max.poll.interval.ms`). Furthermore, Kafka cannot selectively re-queue a single failed task midway through an immutable partition without reprocessing subsequent offsets.
3. **Dynamic Lease Renewals & Targeting (RabbitMQ)**:
   RabbitMQ ack/visibility timeouts do not natively support continuous dynamic lease extensions (worker heartbeats) or targeted cross-process cancellation of specific in-flight executions. PostgreSQL `run_cancels` and row-level `lease_until` timestamps support both seamlessly.
4. **Unified Storage for Live Streaming and Historical Replay**:
   An agent platform must serve both low-latency live SSE streams and historical replays from months ago. Retaining all events indefinitely in Kafka is cost-prohibitive, forcing a dual-write DB pipeline anyway. PostgreSQL `PgEventStream` uses the exact same indexed `execution_events` table for live streaming (`NOTIFY` wake-ups), `Last-Event-ID` disconnect resumption, and historical replays.
5. **Zero New Infrastructure**:
   `SELECT ... FOR UPDATE SKIP LOCKED` delivers hundreds of claims/sec with zero Docker/JVM/Erlang infrastructure dependencies, keeping `jarvis serve` lightweight and instantly runnable in local development.
6. **Future Extensibility**:
   `RunQueue` and `EventStream` are pure Python `Protocol` interfaces in `src/jarvis/ports/`. If scale demands (e.g. 100k+ runs/sec), Redis, SQS, or Kafka adapters can be introduced without altering domain, runtime, or API code.