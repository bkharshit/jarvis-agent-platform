# Future improvements

Findings from the product-wide review on 2026-11-11. Deliberately deferred —
none of these are being worked on now. Items marked **(confirmed)** were
checked against the code; the rest were read from the design and not tested.

## Multi-tenant security hardening

Relevant only once JARVIS is run as a hosted, multi-tenant service. A
single-tenant self-hosted deployment is not exposed to most of these.

1. **Allowlist env credential refs (confirmed).** `EnvCredentialRef` resolves
   any host variable name (`models/credentials.py`, `tools/mcp/connection.py`).
   Combined with a tenant-set `base_url`, a tenant can have any host env var
   sent to their own server as a bearer token. Add an allowlist setting, or
   disable env refs when `JARVIS_AUTH_MODE=required`.
2. **Gate stdio MCP servers (confirmed).** Creating one is command execution
   on the worker; the only gate is the admin role (`api/routes/mcp.py`). Add
   an operator-level setting to enable stdio, and consider sandboxing.
3. **Tenant-scope the scratchpad and conversation keys (confirmed).** The
   `memory_scratch` upsert conflicts on `(agent_id, session_id, key)` with no
   tenant condition, so two tenants on a shared agent overwrite each other.
   `conversations` has the same `(agent_id, session_id)` unique key. Add
   `tenant_id` to both keys.
4. **Reserve platform metadata keys (confirmed).** `RunRequest.metadata` is
   copied to the queue message unfiltered (`api/routes/_run_routes.py`), yet
   the platform stores `sub_agent`, `trigger`, `auto_decline_approvals` and
   `kind` there. Strip or reject these keys at the API boundary.
5. **SSRF guard for outbound URLs.** Model `base_url`, MCP HTTP URLs and
   embedding URLs have no private-range check; only `http_get` has a host
   allowlist.
6. **Webhook ingress (confirmed).** Enforce the size cap while reading the
   body, return the same status for unknown and bad-signature requests, and
   add a signed timestamp for replay protection.

## Reliability and scaling

- **Sub-agent worker starvation.** A parent holds a worker slot while its
  child waits for one. Release the parent's slot during the wait, or reserve
  capacity for child runs.
- **OTLP export blocks the event loop (confirmed).** `OtelSpanSink.export`
  calls the synchronous exporter inline; move it to a thread.
- **Queue fairness.** Claims are plain FIFO by id. Add per-tenant fairness or
  priorities so eval and cron bursts do not delay interactive runs.
- **Atomic trigger firing.** Advance `next_fire_at` and enqueue the run in one
  transaction; give event triggers a real outbox row instead of a post-commit
  callback.
- **Memory window boundaries (confirmed).** `history_window` is a plain slice
  and can begin on an orphaned tool result. Trim to a user-message boundary.
- **Role of injected context.** Retrieved knowledge and conversation
  summaries are injected as `system` messages; use a delimited lower-authority
  message instead.
- **Eval children should auto-decline approvals** so an approval-gated agent
  cannot leave an eval run "running" forever.
- **Resume deadline.** A resumed segment keeps the original absolute
  deadline; with `run_timeout_seconds` set, a long pause probably cancels the
  run on resume. Verify, then extend the deadline by the paused duration.
- **Event volume and retention.** Every `text.delta` is an insert plus
  notify; `execution_events` and `spans` have no retention policy.
- **Vector index.** Chunk search is a sequential scan. Add per-dimension
  partial HNSW indexes when datasets grow.

## Documentation

- The stage deep-dive docs (kept outside this repo) contain illustrative
  "receipts" that were not captured from real runs, and several details that
  contradict the code. Regenerate receipts from real runs or label them as
  examples before publishing.
- `jarvis_deep_dive.md` still describes the Phase 1 in-process design.
