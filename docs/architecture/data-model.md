# Data Model

PostgreSQL (ADR 0001). Payloads are Pydantic models over JSONB (ADR 0002);
scalar filter fields are real columns.

```
agents                mutable pointer row
  id, name UNIQUE, description, current_version, created_at, updated_at

agent_versions        immutable append-only snapshots (replay source of truth)
  id, agent_id FK, version, snapshot JSONB (full AgentDefinition),
  label, created_at, UNIQUE(agent_id, version)

agent_executions      id = run_id
  agent_id, agent_version_id, session_id?, user_id?, trace_id,
  status (PG enum: running|succeeded|failed|cancelled|timed_out),
  input, output JSONB, error?, total_usage JSONB, iterations,
  started_at, finished_at, metadata JSONB
  INDEX(agent_id, created_at DESC), INDEX(session_id)

conversations         UNIQUE(agent_id, session_id), timestamps

messages              conversation_id, execution_id, role (PG enum),
  content JSONB, tool_calls?, tool_call_id?, name?, sequence,
  UNIQUE(conversation_id, sequence)

tool_executions       execution_id, tool_call_id, tool_name,
  arguments JSONB, result JSONB?, is_error, latency_ms

execution_events      durable event log
  cursor BIGSERIAL PK (global monotonic = SSE Last-Event-ID),
  execution_id, event_type, sequence (per-run gapless),
  payload JSONB (full event), created_at,
  UNIQUE(execution_id, sequence)
```

## Snapshot vs reference

- `agent_versions.snapshot` holds the **full AgentDefinition** at publish
  time. A run references `agent_version_id`; edits to an agent never rewrite
  history — `update_and_publish` appends a new version and repoints
  `agents.current_version`.
- Executions therefore replay against the exact definition they ran with.
- Phase 1: `DELETE /agents/{id}` returns 409 if any execution exists.

## Identity & enums

- UUIDs (`uuid` / `uuid_generate_v4()` equivalent via app-side uuid4) for all
  ids; `agent_executions.id` **is** the run_id.
- PG enums for status/role: queryable, constraint-enforced.