# Event Model

Every execution emits a durable, replayable event stream. The event log is
the source of truth for *what happened*; rows in `agent_executions` /
`messages` / `tool_executions` are the queryable projection.

## Envelope

```json
{
  "event_id": "uuid",
  "run_id": "uuid",
  "sequence": 7,          // per-run, gapless, sink-assigned
  "created_at": "2026-09-04T12:00:00Z",
  "type": "tool.call.completed"
}
```

Type-discriminated union `ExecutionEvent` (Pydantic `Literal` discriminators,
`extra="forbid"`):

| type | payload | notes |
|---|---|---|
| `run.started` | agent_id, agent_version_id, session_id? | first event, sequence 0 |
| `iteration.started` | iteration | |
| `model.invocation.started` | attempt | attempt 2+ = retry |
| `text.delta` | text fragment | coalescing is the consumer's choice |
| `model.invocation.completed` | usage, finish_reason, model | |
| `tool.call.requested` | tool_call_id, name, arguments | |
| `tool.call.started` | tool_call_id, name | |
| `tool.call.completed` | tool_call_id, result summary, latency | |
| `tool.call.failed` | tool_call_id, error, kind | tools never crash the run |
| `iteration.completed` | iteration, usage | |
| **`run.completed`** | final_message, total_usage, iterations | terminal |
| **`run.failed`** | error, error_kind, total_usage | terminal |
| **`run.cancelled`** | reason, total_usage | terminal |

`error_kind` ∈ `max_iterations | timeout | model | tool | output_schema`.

## Invariants (unit-tested)

1. **Gapless sequence**: 0,1,2,… per run, assigned inside the sink (single
   asyncio task per run; DB UNIQUE(execution_id, sequence) is the authority
   of record).
2. **Exactly one terminal event** per run. `EventSink.finalize()` takes
   terminal events only, is once-only, and poisons the sink afterwards.
3. **No non-terminal event after a terminal one** — enforced by the sink.

## Replay

- Global DB cursor `execution_events.cursor` (BIGSERIAL PK) = SSE
  `Last-Event-ID`.
- `EventStream.subscribe(run_id, last_cursor)` → replay missed events, then
  live; the terminal event ends the stream.
- Blocking and streaming routes produce identical sequences (guarded by
  test).

## Transport & Delivery (Stage S1 / ADR 0008)

- **`PgEventStream`**: Subscribers tail `execution_events` by global cursor. PostgreSQL `LISTEN/NOTIFY` acts strictly as an interrupt/wake-up signal (with a 1s fallback poll), ensuring a unified storage engine for both live SSE streaming and historical replays.
- **Resilience**: Delivery is exactly-once from PostgreSQL; missed `NOTIFY` signals cost at most 1s of latency, never data correctness. See [ADR 0008](../adr/0008-distributed-runs.md) for alternatives considered (e.g. why PostgreSQL was chosen over Kafka or RabbitMQ).