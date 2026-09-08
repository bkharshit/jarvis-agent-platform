# ADR 0012 — MCP tool providers

- **Status**: Accepted (planning; precedes the S4 build session)
- **Date**: 2026-09-08
- **Amends**: `ports/repository.py` gains one Protocol (§5); nothing else in
  `ports/` changes. Rides ADR 0005 (env-var indirection), ADR 0009 (tenancy
  scoping pattern), ADR 0011 §consequences (per-call approval already
  anticipates MCP), ADR 0004/D28 (resolution inside the runtime's try).

## Context

Roadmap §S4 sketches MCP servers as tool providers riding `ports/tools.py`:
"`McpToolProvider` implements `ToolRegistry`… `ToolBinding.config` carries
server/transport settings". That sketch predates S2 (tenancy) and S3
(registries-as-platform-config with allow-lists), and its acceptance item —
"Tools → MCP (server management)" — cannot ship honestly against per-agent
inline transport config: there would be no server to manage, only N agents
each embedding a copy of the connection details.

Meanwhile the seams the sketch assumed are all in place and tested:

- `Tool` / `ToolRegistry` / `ToolRuntime` (`ports/tools.py`) — the
  template-method envelope gives any `BaseTool` validation, per-tool timeout,
  cooperative cancellation, truncation, and exception → error-result
  conversion for free.
- `ToolBinding` (`domain/agent.py:45`) — name + free-form config; unknown
  names are skipped at prompt-build, never fatal
  (`agent_runtime.py:_bound_descriptors`).
- `error_kind="tool"` already exists on the frozen envelope
  (`domain/events.py:149`, `agent_runtime.py:58`) — no new event type, no
  new kind value needed for this stage.
- D28 (S2): resolution that can fail must sit inside `AgentRuntime.run`'s
  try so the failure becomes a persisted terminal state, never an exception
  the worker would treat as a claim failure and retry forever.
- ADR 0011's closing consequence: "S4 (MCP tools) inherits per-call gating
  without another contract change."

## Decision

### 1. MCP servers are rows, not binding config

A new `mcp_servers` table (migration 0007) holds server definitions,
tenant-scoped the way agents are: `tenant_id` nullable, NULL = platform-shared
and visible to every tenant; tenant-owned rows shadow same-name shared rows.
Fields: id, tenant_id, name, enabled, config (typed JSONB, ADR 0002),
timestamps; name unique per tenant, a slug (`^[a-z0-9][a-z0-9-]*$`) so
`mcp__<name>__<tool>` is a sane model-visible identifier. Name is immutable
once created (PATCH covers `enabled` and `config` only) — the name is the
join key from thousands of version snapshots, and a rename would silently
orphan every binding referencing it.

Agents do **not** embed transport config. `ToolBinding` is unchanged: an
agent binds discovered tools by name (`mcp__<server>__<tool>`) exactly the
way it binds `calculator` or `http_get`. This is the `credential_ref`
precedent applied to servers (ADR 0002's snapshot-vs-reference): the
immutable version snapshot pins a *reference* (the tool name), and the
platform resolves server connectivity at run time. Snapshots stay valid when
a server's URL or command changes; two agents share one server; the server's
lifecycle (disable, fix, re-point) is managed in one place. The roadmap
sketch's `ToolBinding.config` carrying transport settings is **rejected**
with this reasoning recorded.

### 2. Tool identity: discovery is not exposure

`McpToolProvider` connects (stdio subprocess or streamable HTTP), lists
tools, and maps each to a `ToolDescriptor` named `mcp__<server>__<tool>`.
MCP tool names are sanitized to `[A-Za-z0-9_-]` (other characters → `_`,
first-wins on collisions). No tool reaches a model unless the agent's
enabled bindings name it — **binding selection is the allow-list** (the
`http_get`/D35 philosophy restated: opt-in, and discovery alone grants
nothing). A hallucinated or drifted name degrades exactly like an unknown
builtin today: skipped at prompt-build; a model call to it returns the
ToolRuntime's "unknown tool" error result, recoverable, run continues.

One deviation from the builtin default, deliberate: **MCP descriptors
default `annotations.requires_approval=True`**. An MCP server is an
external, arbitrary side-effect surface (D29's existence-leak logic does
not cross process boundaries); per ADR 0011 the human gets a per-call
decision. A binding overrides with `config: {requires_approval: false}` —
binding config wins over descriptor annotations, unchanged from S10.

### 3. Secrets stay references (ADR 0005, unchanged pattern)

The typed config union carries env-var *names*, never values:

```jsonc
{"type": "stdio", "command": "python", "args": ["-m", "weather"],
 "env": {"WEATHER_TOKEN": {"type": "env", "env_var": "MCP_WEATHER_TOKEN"}}}
{"type": "http", "url": "https://example.com/mcp",
 "headers": {"Authorization": {"type": "env", "env_var": "MCP_EXAMPLE_TOKEN"}}}
```

`EnvCredentialRef` is reused as-is (the union member from `domain/agent.py`).
Values resolve from the process environment at connect time; a missing
variable is a resolution failure with the variable's name in the message.
The API echoes config (names only — safe, same as model refs); env-ref
values never enter the DB, a snapshot, a log line, or a response. Stdio
subprocesses get a minimal environment: `PATH` plus the referenced
variables only, never the parent process's wholesale environment.

### 4. Resolution semantics (the D28 pattern, third application)

Each run segment resolves its MCP toolset **eagerly, inside the runtime's
try**, before prompt build:

- Group the agent's enabled `mcp__*` bindings by server name; fetch rows
  through the tenant-scoped repo (`ctx.tenant_id`, shared-NULL visible,
  tenant-owned shadowing shared — the agents repo's `_shared_visible`
  pattern verbatim).
- Connect each referenced server (per-segment connections; stdio spawns a
  subprocess, HTTP opens a session), list tools, build descriptors.
- Failure — server row missing, disabled, unreachable, auth env var absent,
  protocol error — becomes a **persisted terminal `run.failed` with
  `error_kind="tool"`** naming the server. Same rule as model resolution
  (D28) and strategy resolution (D36): the boundary fails honestly, in one
  place, before any token is spent. A run whose bound toolset cannot be
  provided is misconfigured; producing a run that errors mid-flight instead
  would hide the misconfiguration behind model behavior.
- **Per-call failures mid-run are recoverable**: a server that dies, times
  out, or errors on `tools/call` after a successful resolve surfaces as a
  normal error `ToolResult` via the ToolRuntime envelope — the model sees
  the failure and continues. Resolution is the boundary; execution is not.
- Connections close with the segment in a `finally` — completed, failed,
  cancelled, or paused. A resumed segment (S10) re-resolves from its
  snapshot and reconnects; nothing about a pause persists a connection.
- Agents with no `mcp__*` bindings resolve nothing — the effective toolset
  is the builtin registry, byte-identical behavior to today.

### 5. The rule-7 change list

- `ports/repository.py` — gains one Protocol: `McpServerRepo` (create, get,
  get_by_name, list_servers, update, delete — all tenant-scoped like
  `AgentRepo`). This is the pre-declared change this ADR carries; the
  domain type it references (`domain/mcp.py`) is pure pydantic.
- `ports/tools.py`, the event union, the error shape, `AgentDefinition`,
  `ToolBinding`, the resume contract — **untouched**. No migration touches
  existing tables; no existing snapshot or YAML changes meaning.
- New API surface (additive): `/v1/mcp/servers` CRUD + probe
  (§consequences). `GET /v1/capabilities` flips `tools.detail.mcp` from
  `{enabled: false, stage: "S4"}` to a derived fact.

### 6. The SDK is an adapter detail, pinned and swappable

The official MCP Python SDK, **v2 line (`mcp>=2.0,<3`, 2026-07-28
spec revision)**, wrapped in one internal module (`src/jarvis/tools/mcp/`)
that implements our seams: `McpServerConnection` (connect / descriptors /
call / close), `McpResolutionError`, and the per-run registry view. The
runtime, API, and tests never import the SDK directly — unit tests fake the
connection seam, so the suite stays hermetic and the SDK can be swapped or
shimmed without touching the runtime (adapters swappable by construction).
v2 facts that shape the adapter (from the SDK migration guide): the unified
`Client` API, snake_case result fields (`is_error`, `input_schema`),
`call_tool` **raising** `MCPError` on JSON-RPC errors, and cancelled
requests no longer being answered (matches our cooperative cancellation).

## What stays frozen

- The event envelope, terminal semantics, the queue contract, and the
  resume contract (ADR 0003/0008/0010/0011) — MCP tools are ordinary
  `tool.*` events, ordinary `tool_executions` rows, ordinary pending calls
  in a pause event. Replay ≡ live with zero changes.
- `AgentRuntime`'s ownership (ADR 0004): the loop, limits, deadline,
  cancellation, and terminal emission stay in the runtime; the provider
  only resolves tools. MCP tools get validation/timeout/cancellation from
  `ToolRuntime` because they are `BaseTool`s — no envelope is duplicated.
- Tenancy: server management is admin/owner-only (the members-route
  pattern); foreign-tenant ids 404 (D29, no existence leak). Members may
  list and probe their tenant's servers (binding editors need descriptors;
  the creation boundary is the gated one).

## Consequences

- **Migration 0007** — one new table; no existing data changes.
- **API** — `/v1/mcp/servers` (list, create, get, patch, delete) and
  `/v1/mcp/servers/{id}/probe` (connect fresh, return discovered
  descriptors, persist nothing). Errors ride the frozen envelope (404
  foreign, 403 role, 409 duplicate name, 422 config).
- **Capabilities** — `tools.detail.mcp` becomes
  `{enabled: true, servers: [...]}` derived from the repo at request time;
  the Tools page flips its MCP panel on, the agent editor gains an MCP
  tool picker (probe on demand), and `mcp__*` bindings list alongside
  builtins.
- **Workers connect MCP servers** — resolution happens inside
  `runtime.run`, so the worker process owns the subprocesses/sessions;
  nothing changes in the queue contract. `worker_concurrency` bounds
  concurrent MCP sessions transitively.
- **Deferred** (§risks of the implementation plan): connection pooling and
  descriptor caching (each segment reconnects and re-lists), OAuth flows
  for remote servers (static env-ref headers only), sampling/roots/logging
  client features (tools only), hot enable/disable (a disabled server
  fails resolution on the next segment — honest, no cache to invalidate),
  tool-name length beyond provider function-name limits (documented, not
  truncated).