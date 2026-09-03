# API Surface

FastAPI, prefix `/v1`. Controllers are thin: parse → resolve from
`AppContainer` (deps) → call runtime/repos → map errors. SSE framing lives in
one place (`api/sse.py`).

```
POST   /v1/agents                    201 (also publishes version 1)
GET    /v1/agents?limit&offset       list
GET    /v1/agents/{id}               definition + version index
PATCH  /v1/agents/{id}               update → auto-publish new AgentVersion
DELETE /v1/agents/{id}               204 / 409 if executions exist
GET    /v1/agents/{id}/versions/{n}  frozen snapshot

POST   /v1/agents/{id}/run           blocking: {input, session_id?, user_id?,
                                       variables?, metadata?} → RunResult
POST   /v1/agents/{id}/stream        SSE: id: <cursor>, event: <type>,
                                       data: <event JSON>; Last-Event-ID resume
                                       (replay after cursor, then live);
                                       terminal event ends the stream
POST   /v1/executions/{id}/cancel    idempotent; triggers the run's token

GET    /v1/executions?agent_id&status&session_id
GET    /v1/executions/{id}           run + messages + tool_executions
GET    /v1/executions/{id}/events?after   JSON replay (or SSE via Accept)
GET    /v1/conversations/{agent_id}/{session_id}/messages
```

## Error envelope

```json
{"error": {"kind": "<error_kind|not_found|conflict|validation>",
           "message": "...", "details": {...}}}
```

Mapped from the domain taxonomy in `api/errors.py` — one place, consistent
HTTP codes (`404` not found, `409` conflict, `422` validation, `502` model
upstream, `408`/deadline → mapped per taxonomy).

## Streaming contract

- `id:` = durable `execution_events.cursor`; `event:` = event type;
  `data:` = full event JSON.
- Client reconnects with `Last-Event-ID: <cursor>` → server replays events
  with cursor > given, then goes live. Exactly-once on resume (tested).
- Blocking `/run` and `/stream` invoke the same `AgentRuntime.run()`; the
  stream endpoint spawns the run as an asyncio task and subscribes to the
  sink.

## DI

`api/deps.py` builds one `AppContainer` (settings → engine/session factory →
repos → sink → registries → runtime) at startup; FastAPI dependencies hand
out components. No globals, no import-time side effects.