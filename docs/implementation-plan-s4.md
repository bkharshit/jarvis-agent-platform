# JARVIS — Implementation Plan (Stage S4: MCP tools)

- **Status**: Planned (this doc + ADR 0012 + D37/D38 precede the build
  session; no S4 code exists yet)
- **Date**: 2026-09-08
- **Rides on**: ADR 0012 (this stage's contract), ADR 0005 (env-var
  indirection for server secrets), ADR 0009 (tenant scoping, shared-NULL
  visibility, role gates), ADR 0004 + D28 (resolution inside the runtime's
  try; the orchestrator owns limits), ADR 0002 (typed JSONB config), D36
  (the boundary-fails-honestly precedent), ADR 0011 (per-call approval MCP
  inherits), roadmap §S4.
- **Design doc**: `docs/adr/0012-mcp-tool-providers.md` — the plan below
  is the commit-by-commit execution of it. Where they differ, the ADR wins.

## Context

Agents can call exactly three builtin tools. The rest of the world's tools
live behind MCP servers (stdio subprocesses and streamable HTTP). S4 turns
each configured server's toolset into ordinary JARVIS tools — discovered,
wrapped in `BaseTool`, executed through the same `ToolRuntime` envelope —
so validation, per-tool timeout, cooperative cancellation, truncation,
`tool.*` events, `tool_executions` rows, replay, and per-call approval all
apply to MCP **for free** (that is the payoff of the Phase 1 template-method
design; roadmap §S4 says exactly this).

Two roadmap-sketch deviations are recorded in the ADR: servers are
**tenant-scoped DB rows**, not per-binding transport config (the
"server management" UI item demands it; the snapshot pins tool *names*,
not connectivity — the `credential_ref` precedent), and **discovery is not
exposure** — binding selection is the allow-list.

## Verified seams (read in code, 2026-09-08)

- `ports/tools.py` — `Tool`, `ToolRegistry`, `ToolRuntime` Protocols;
  untouched by this stage. `BaseTool` (`tools/base.py`) implements the
  template method an MCP wrapper subclasses.
- `tools/runtime.py:33` — `ToolRuntime.execute`: registry lookup,
  jsonschema validation against the descriptor, per-tool timeout
  (descriptor `annotations.timeout` > binding `config.timeout` >
  default 30s), `raise_if_cancelled`, exception → error `ToolResult`
  (`kind: validation|timeout|internal`), 50k truncation. All of it
  applies to an MCP wrapper unmodified.
- `tools/registry.py:17` — `InMemoryToolRegistry`; duplicates raise. A
  per-run registry view can literally be a fresh `InMemoryToolRegistry`
  seeded with the builtin instances + MCP wrappers — no new abstraction.
- `domain/agent.py:45` — `ToolBinding{name, enabled, config}` free-form;
  `agent_runtime.py:_bound_descriptors` **skips unknown names silently**
  (the drift-tolerance precedent for dynamic toolsets); S10's
  `_approval_required` reads `binding.config["requires_approval"]` first,
  descriptor `annotations` second.
- `domain/events.py:149` + `runtime/agent_runtime.py:58` —
  `error_kind="tool"` already exists in both Literals. No envelope change.
- `runtime/agent_runtime.py:158-198` — `run()`'s try resolves the model
  (D28), catches typed errors before the blanket `except Exception`
  (which mislabels to `model`). `resume()` mirrors it (lines 672-714).
  MCP resolution slots in as the third in-try resolution, its failure
  caught before the blanket handler.
- `runtime/agent_runtime.py:202-247` — `_execute` builds the prompt from
  `_bound_descriptors(agent)` — descriptors must exist before
  `PromptEngine.build`; MCP resolution therefore happens before `_execute`
  body, inside `run()`'s try.
- `runtime/worker.py` — claims → `runtime.run()`. No worker changes: the
  worker process owns MCP connections transitively because resolution is
  in the runtime.
- `persistence/repositories.py:78-82` — `_shared_visible`: `(column ==
  tenant_id) | column.is_(None)`; the agents repo's tenant pattern to copy
  for `SqlMcpServerRepo`.
- `api/routes/members.py:18-25` — `_require_member_manager` /
  `can_manage_members`: the admin/owner gate pattern for server
  management routes; foreign ids 404 (D29).
- `api/routes/capabilities.py:121-122` — `tools.detail.mcp` today is
  `{"enabled": False, "stage": "S4"}` — the exact flag this stage flips,
  with a derived detail replacing the literal.
- `api/deps.py:83-122` — one wiring point: the container builds the
  registry and the runtime; `McpServerRepo` + the provider slot in here.
- `config.py` — no MCP settings yet; the CSV `NoDecode` validator pattern
  (D35) is the reference if a list-valued setting were needed (none is).
- `web/src/sections/tools/ToolsPage.tsx` + `capabilities/detail.ts` —
  `mcpGate` already reads `tools.detail.mcp`; the page renders the
  not-enabled copy from it. The panel swap is payload-driven.
- `web/src/sections/settings/*` — the admin-panel pattern (Members /
  ApiKeys / Credentials) to mirror for server management UI.
- Tests: unit suite is hermetic (no DB, no network, no subprocess needed
  for the fake-seam path); integration (`-m db`) self-bootstraps
  `jarvis_test`; `make gen-api` regenerates the TS client from the API.

## Confirmed decisions (recorded as D37/D38, ADR 0012 carries the contract)

### D37 — MCP servers are tenant-scoped rows; agents bind tool names

`mcp_servers` table (migration 0007): id, tenant_id (NULL = shared,
agents-repo visibility), immutable slug name unique per tenant, enabled,
typed JSONB config (`McpStdioConfig | McpHttpConfig` union carrying
`EnvCredentialRef` values, ADR 0005), timestamps. `ToolBinding` is
unchanged — agents bind `mcp__<server>__<tool>` like any builtin; the
snapshot pins the name, the platform resolves connectivity at run time
(the `credential_ref` precedent; roadmap sketch's inline transport config
rejected). Server management is admin/owner-only; members list/probe;
foreign tenant 404s (D29). Name immutability keeps thousands of snapshots
joinable; `enabled` is the honest kill switch (bound tools fail
resolution while off).

### D38 — Eager per-segment resolution inside the runtime's try

Each run segment (fresh or resumed) groups enabled `mcp__*` bindings by
server, connects, lists tools, and builds a per-run registry view
(builtins + MCP wrappers) before prompt build — inside the try, after
model resolve (D28 pattern, third application). Resolution failure
(missing/disabled/unreachable server, absent env var, protocol error) →
exactly one persisted terminal `run.failed` with `error_kind="tool"` naming
the server — never an exception past the runtime, never a worker retry
loop. Per-call failures after resolve stay recoverable error `ToolResult`s
via the ToolRuntime envelope. Connections close with the segment (finally:
completed/failed/cancelled/paused); resume segments re-resolve and
reconnect. No `mcp__*` bindings → no-op, byte-identical behavior to today.
MCP descriptors default `annotations.requires_approval=True` (binding
config can ungate) — external servers are an arbitrary side-effect
surface and ADR 0011 already gives the human per-call verdicts.

## Design

### 1. Domain — `src/jarvis/domain/mcp.py` (new)

Pure pydantic, `extra="forbid"`, no IO (dependency rule):

- `McpStdioConfig{type: "stdio", command: str, args: list[str] = [],
  env: dict[str, EnvCredentialRef] = {}}`
- `McpHttpConfig{type: "http", url: str (http/https, validated),
  headers: dict[str, EnvCredentialRef] = {}}` — discriminated union
  `McpServerConfig`.
- `McpServer{id, name (slug validator), config: McpServerConfig,
  enabled: bool = True, tenant_id: str | None, created_at, updated_at}`.

`EnvCredentialRef` is imported from `domain/agent.py` — one ref type, the
same "name, never value" rule. Config validation at the domain edge gives
both the API 422 and the repo a typed boundary (ADR 0002 pattern).

### 2. Port + persistence — `McpServerRepo`

- `ports/repository.py` gains the Protocol (ADR 0012 §5 — the pre-declared
  ports change): `create`, `get(server_id, *, tenant_id)`,
  `get_by_name(name, *, tenant_id)`, `list_servers(*, tenant_id)`,
  `update(server)`, `delete(server_id, *, tenant_id) -> bool`. Same
  keyword-tenant shape as `AgentRepo`; `mypy --strict` on ports applies.
- `persistence/repositories.py` gains `SqlMcpServerRepo` + `McpServerRow`
  with `_shared_visible` scoping copied from the agents repo (tenant rows
  shadow same-name shared rows: order by `tenant_id IS NULL` in
  `get_by_name`, or filter owned-first).
- **Migration 0007** — `mcp_servers`: `id uuid pk`, `tenant_id uuid null
  fk → tenants(id)`, `name text not null`, `enabled boolean not null
  default true`, `config jsonb not null`, `created_at/updated_at`. Unique
  per tenant incl. NULL: partial unique index on `(tenant_id, name)` where
  tenant_id is not null **plus** a unique `name` where tenant_id is null
  (Postgres NULLs-are-distinct make a single composite index insufficient —
  follow whatever the agents/tenancy migrations already do for this case;
  check `0004_tenancy.py` at build time). `op.create_table` emits any
  needed types itself (sa.Enum gotcha D4 — no enums needed here).

### 3. Connection adapter — `src/jarvis/tools/mcp/` (new, the only SDK touchpoint)

- `connection.py`: `McpServerConnection` — async context manager over the
  SDK v2 `Client`. `connect()` resolves env refs from the process env
  (missing name → `McpResolutionError` naming it), builds the transport:
  - stdio → subprocess with env = `{PATH} ∪ referenced vars` only;
  - http → headers with resolved values (values used, never stored).
  `descriptors()` → `tools/list` mapped to `ToolDescriptor`s
  (`mcp__<server>__<tool>`, sanitized `[A-Za-z0-9_-]`, first-wins on
  collision, `input_schema` → `parameters`,
  `annotations={"requires_approval": True, "timeout": None}`);
  `call(raw_tool, arguments) -> str` → `tools/call`, text content blocks
  joined, non-text blocks summarized (`[image content omitted]`),
  `is_error` → `ValueError` with the server's message (ToolRuntime
  converts to an error result), SDK `MCPError` → `ValueError` likewise.
- `errors.py` or same module: `McpResolutionError(Exception)` — the one
  internal exception the runtime catches before its blanket handler.
- `provider.py`: `McpToolProvider(repo, settings)` —
  `async resolve(bindings, tenant_id) -> McpTooling`: groups `mcp__*`
  names by server, loads rows (tenant-scoped), returns a dataclass
  `{registry: InMemoryToolRegistry (builtins + wrappers), aclose: callable}`.
  Raises `McpResolutionError` with the server name on any failure; no
  bindings → returns a view over the builtin registry with nothing open.
  Non-`mcp__` bindings are not its concern.
- `McpTool(BaseTool)` per discovered tool: `descriptor` from the mapping,
  `_execute` delegates to `connection.call`.
- SDK pin: `mcp>=2.0,<3` (+ its `mcp-types` companion) added to
  `[project.dependencies]` — it is a runtime dependency (workers connect
  during runs), not a dev extra. v2 facts baked into the adapter: unified
  `Client`, snake_case fields, `call_tool` raising `MCPError`, cancelled
  requests not answered. If v2's exact `Client` surface differs from the
  migration-guide reading (some sections were truncated in research),
  the adapter is the only file that adapts — that is why it exists.
- Settings: `mcp_connect_timeout: float = 15.0` (the one new knob;
  per-call timeouts already ride binding `config.timeout` through
  ToolRuntime).

### 4. Runtime — resolution slots into `run()`/`resume()` (D38)

- `AgentRuntime.__init__` gains `mcp: McpToolProvider | None = None`
  (None = no MCP configured; unit tests of today's behavior construct it
  exactly as now).
- In `run()`'s try, after model resolve:
  `tooling = await self._resolve_tooling(agent, ctx)` — a no-op fast path
  (`self._tooling_default()`) when the snapshot has no `mcp__*` bindings.
  In `resume()`, identically, before `_resume_segment`.
- `except McpResolutionError` **before** the blanket `except Exception`
  in both methods → `_terminal_failed(..., error_kind="tool", ...)`.
- `_execute`/`_loop`/`_bound_descriptors` take the per-segment tooling
  (registry + a `ToolRuntime` over it) instead of reading
  `self._tools`/`self._tool_runtime` — a mechanical threading refactor;
  every existing call site either passes the default tooling or the
  resolved one. Loop logic, events, pause/resume branches untouched.
- Session close: `_execute`/`_resume_segment` wrap the loop in
  `try/finally: await tooling.aclose()` — closed on complete, fail,
  cancel, and pause alike (a paused segment re-resolves on resume).
- Resume rebuild note: `_rebuild_messages` already re-renders the system
  prompt from `_bound_descriptors` — it must see the segment's resolved
  tooling so the prompt matches the fresh segment's tools.

### 5. API — `api/routes/mcp.py` (new) + schemas + capabilities

- `POST /v1/mcp/servers` (admin/owner): `{name, config, enabled?}` →
  201 `McpServerOut` (id, name, config — env names only, enabled,
  tenant_id, timestamps). 409 duplicate name; 422 bad slug/config.
- `GET /v1/mcp/servers` (any tenant member) — list.
- `GET /v1/mcp/servers/{id}` — 404 foreign/missing (D29).
- `PATCH /v1/mcp/servers/{id}` (admin/owner) — `enabled` and `config`
  only (name immutable); 422 if a patch tries to move the name.
- `DELETE /v1/mcp/servers/{id}` (admin/owner) — 204; agents keep their
  snapshots and fail resolution honestly on the next run.
- `POST /v1/mcp/servers/{id}/probe` (any tenant member) — connects fresh
  through the provider's connection path (no registry side effects),
  returns `{server: McpServerOut, tools: [ToolDescriptor...]}`; a
  resolution failure is a 502 `mcp_unreachable` in the frozen error
  envelope (the members/credentials routes' error style).
- Anonymous mode (default tenant, full access) works end-to-end — the dev
  walkthrough needs no auth setup.
- `capabilities.py`: `tools.detail.mcp` becomes
  `{"enabled": True, "servers": [{id, name, transport, enabled}]}` derived
  from the repo at request time (registry-mirrors-registry; counts only,
  no live connections from capabilities).
- `api/schemas.py`: `McpServerCreate/Out/Patch`, `McpProbeResponse`;
  `make gen-api` regen lands the TS types.

### 6. Fixture MCP server + tests (hermetic discipline)

- **`tests/fixtures/mcp/server.py`** — a real MCP server on stdio using
  the same SDK (`MCPServer`, v2 name; stdio transport): two tools —
  `echo(text)` and `add_numbers(a, b)` — one of which returns a text
  block; deliberately simple schemas. Launched as
  `[sys.executable, tests/fixtures/mcp/server.py]` — a subprocess, but
  hermetic (local, no network).
- **Unit** (fake the connection seam — no subprocess, no SDK client):
  domain config union + slug validation; env-ref resolution (missing var
  names the variable); descriptor mapping/sanitization/collision;
  content joining + `[image omitted]` + is_error → ValueError; provider
  grouping (two servers, shared+owned shadowing, disabled server, missing
  server, no-mcp no-op); runtime through the real loop with the mock
  provider: resolution failure → exactly one terminal `run.failed`
  (`error_kind="tool"`, server named, worker acks — no retry), call
  failure mid-run → error ToolResult + run completes, approval default →
  pause before any `tool.call.started` + ADR 0011 `decisions` resume
  executes the approved MCP call, resume segment re-resolves, no-mcp
  agent's event stream identical to today (existing suite is the proof —
  it must not change by one event).
- **Integration** (`-m db`): repo CRUD + tenancy (owned/shared/404);
  API routes (201/409/404/403/422, anonymous-mode happy path, probe
  against the fixture stdio server through the HTTP API); capabilities
  detail; **the roadmap acceptance e2e**: create server row → create
  agent binding `mcp__fixtures__add_numbers` → `POST /run` (mock
  provider scripted to call it) → assert `tool_executions` rows, events,
  and transcript identical in shape to a builtin run; the same through
  `/stream` and through a pause→`decisions`→resume chain.
- `test_migrations.py`: 0007 up/down cycle.

### 7. Web (UI enablement — the stage's last item)

- `make gen-api` regen (new client methods + types).
- **ToolsPage MCP panel**: servers from `tools.detail.mcp` (name,
  transport badge, enabled state), "Add server" dialog (stdio/http forms
  → POST; 403 renders the honest members-panel-style error), per-server
  "View tools" → probe → descriptor cards like the builtins listing,
  enable/disable toggle (PATCH), delete with confirm. `mcpGate` flip:
  the coming-soon copy is replaced by the panel when `enabled`.
- **Agent editor**: an MCP tools picker next to the builtin toggles —
  server dropdown → probe → `mcp__server__tool` checkboxes become
  ordinary `ToolBinding` rows; `requires_approval` toggle in the binding
  config editor (existing pattern, now defaulting visible-on for MCP).
- `TEST_CAPABILITIES` seed gains `tools.detail.mcp = {enabled: true,
  servers: [...]}`; vitest: panel renders from payload, probe interaction
  (msw), add-dialog validation, editor picker writes bindings, payload
  swap follows (the S3 payload-swap test style).
- Admin gating mirrors the Settings panels (UI hides when whoami says
  member; the API remains the enforcer — rule 6).

### 8. Rule-7 audit (what touches what)

- `ports/repository.py`: + `McpServerRepo` Protocol — **the only ports
  change**, pre-declared by ADR 0012. `ports/tools.py`, `ports/queue.py`,
  `ports/strategy.py`, `ports/model.py`, `ports/credential.py`,
  `ports/events.py`: untouched.
- Event envelope: untouched (no new event types; `error_kind="tool"`
  already exists — the D36 precedent in reverse: this stage adds *no*
  kind value at all).
- Error shape: untouched (`mcp_unreachable` reuses the frozen envelope).
- Domain: additive (`domain/mcp.py`); `AgentDefinition`/`ToolBinding`
  byte-compatible — every existing snapshot and YAML stays valid, no
  data migration.
- New internals: `tools/mcp/` (SDK adapter), `api/routes/mcp.py`,
  `SqlMcpServerRepo`, migration 0007, one Settings field.

## Commit sequence (each independently green)

Gates before **every** commit: `pytest tests/unit`, `ruff check src
tests`, `mypy src`; web commits also `tsc --noEmit`, eslint, vitest.

1. **docs: stage record** — ADR 0012, this plan, D37/D38 in
   `docs/decisions.md` (this commit).
2. **feat(domain,persistence): server registry** — `domain/mcp.py`,
   `SqlMcpServerRepo` + row, migration 0007, `McpServerRepo` port,
   repo tenancy tests (integration) + domain unit tests + migration cycle
   test.
3. **feat(tools): MCP connection adapter** — `tools/mcp/connection.py`
   + provider + `McpTool`, `mcp>=2.0,<3` dependency,
   `mcp_connect_timeout` setting; unit tests over the fake seam
   (env refs, mapping, content, error conversion, provider grouping).
4. **feat(runtime): eager in-try resolution** — `McpResolutionError`
   catch before the blanket handler in `run()`/`resume()`, tooling
   threading refactor, segment-scoped close, approval default; unit tests
   through the real loop (terminal tool failure + worker ack, recoverable
   call failure, pause→decisions→resume, no-mcp unchanged).
5. **feat(api): server routes + capabilities flip** — `/v1/mcp/servers`
   CRUD + probe, schemas, capabilities derived detail, auth/tenancy
   integration tests, anonymous-mode happy path.
6. **test(integration): acceptance e2e** — fixture stdio MCP server;
   agent runs `mcp__fixtures__add_numbers` through `/run` + `/stream` +
   pause/resume with rows/events asserted identical in shape to builtins
   (the roadmap acceptance line).
7. **feat(web): Tools → MCP** — gen-api regen, ToolsPage MCP panel +
   probe, agent-editor MCP picker, capabilities types/seed, vitest.
8. **docs: closure** (after Harshit live-verifies) —
   `docs/walkthrough-s4.md`, README MCP section, roadmap §S4 shipped
   note, decisions §4 deferral list updated (S3 + S4 landed),
   gotchas/CLAUDE.md notes for whatever the build finds.

## Risks & deliberate deferrals

- **SDK v2 freshness** — released 2026-07-28; the adapter is one module
  precisely so surface surprises (exact `Client` kwargs, in-memory test
  transport name) are contained. Fallback: pin v1 line
  (`mcp>=1.28,<2`, `ClientSession` shape) — same seams, different adapter
  file; the ADR's contract is SDK-agnostic.
- **No connection pooling / descriptor caching** — every segment
  reconnects and re-lists (a stdio spawn per run). Honest cost, simple
  lifecycle; pooling is a contained future optimization inside
  `tools/mcp/`.
- **No OAuth for remote servers** — static env-ref headers only;
  `OAuthClientProvider` is a contained future adapter extension.
- **No sampling/roots/logging MCP client features** — tools only
  (roadmap scope; those are server→client capabilities).
- **Disabled/deleted server fails runs at resolution** — deliberate
  (fail honestly at the boundary; a run that needs a tool it can't have
  shouldn't start). The error names the server; the executions UI shows
  `error_kind: tool`.
- **Tool-name length** — `mcp__<server>__<tool>` can exceed some
  providers' 64-char function-name limit; not truncated (uniqueness
  beats compatibility), surfaced as a probe/`_bound_descriptors`-time
  warning in the UI copy; real providers increasingly accept longer
  names.
- **CLI deferred** — no `jarvis mcp` command this stage (no
  chicken-and-egg: the API is usable from boot in anonymous mode; the
  S2 CLI existed because bootstrapping *needed* it). Park with the other
  parked UI/CLI ideas if wanted.
- **Live third-party server demo** — like the S3 `friendly-greeter`
  demo, optional walkthrough material (any public stdio/http MCP
  server), not a stage gate; the fixture is the gate.
- **Anti-scope** (frontend-architecture table): knowledge/RAG tooling
  (S8 rides *on* this stage's tool family), multi-agent (S14), per-server
  tool allow-lists beyond binding selection, hot reload.

## Verification (S4 exit criteria — roadmap §S4 acceptance)

- e2e with a fixture MCP server exposing a tool: an agent uses it through
  `/run` like a builtin; tool executions appear in `tool_executions`
  rows identically to builtins (integration test + live walkthrough).
- A run whose bound server is missing/disabled/unreachable ends in
  exactly one persisted terminal `run.failed` with `error_kind="tool"`
  naming the server; the worker acks; no route 500s; no retry loop.
- MCP tool calls get validation/timeout/cancellation/truncation from
  `ToolRuntime` with zero envelope code (asserted by a timeout test and a
  recoverable-failure test, not by inspection).
- Approval: an MCP call pauses with `requires_approval` by default;
  ADR 0011 `decisions` resume approves/declines per call with no new
  contract (regression-tested through the resume path).
- `GET /v1/capabilities` reports `tools.detail.mcp` truthfully; the
  Tools page manages servers; the agent editor binds `mcp__*` names
  alongside builtins; nothing renders data the API didn't return.
- Agents without MCP bindings behave identically to today (the untouched
  existing suites are the regression proof).
- Gates green at every commit: unit, ruff, mypy; tsc, eslint, vitest at
  commits 5 (regen) and 7.

## Walkthrough draft (for `docs/walkthrough-s4.md`, live session)

0. `uv sync` (SDK in) → restart `uv run jarvis serve` → capabilities
   shows `tools.detail.mcp` enabled with an empty server list.
1. Web Tools → MCP: add the fixture server (stdio:
   `python tests/fixtures/mcp/server.py`) via the Add dialog; probe lists
   `mcp__fixtures__echo` / `mcp__fixtures__add_numbers`.
2. Agent editor: bind `mcp__fixtures__add_numbers` (+ calculator), save;
   run "what is 23 plus 19?" on gemma4:31b — watch the tool call stream
   like a builtin; executions detail shows the `tool_executions` row.
3. Approval: bind with the default gating, run — the pause card offers
   per-call Approve/Reject (ADR 0011); decline → refusal message, run
   continues without the tool.
4. Kill switch: disable the server (toggle), run again — immediate red
   `run.failed`, `error_kind: tool`, server named. Delete the server —
   same honest failure from the pinned snapshot.
5. (Optional) a real third-party MCP server over streamable HTTP with an
   env-ref header — the `friendly-greeter`-style live proof.