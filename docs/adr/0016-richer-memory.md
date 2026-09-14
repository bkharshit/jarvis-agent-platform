# ADR 0016 — Richer memory (S12): rolling summaries and a per-session scratchpad

Status: accepted (planning ADR — no S12 code exists at time of writing)
Date: 2026-09-14
Supersedes: none (implements the roadmap S12 sketch; vector memory is
deferred to S8 with its design note recorded here, §7)

## Context

Phase 1 ships one memory shape: a flat last-N conversation window
(`MemoryConfig.max_messages`, sliced in `PromptEngine.history_window`).
The roadmap S12 sketch calls for three independently shippable items:
summarization/compaction, vector memory, and a working-memory scratchpad.
Memory is a strategy-adjacent concern the orchestrator already delegates
(`_load_memory` → `ConversationRepo`), and conversation history is already
separated from the prompt build — so all three ride existing seams.

Verified seams this design rides on (paths checked 2026-09-14):

- `domain/agent.py:51` `MemoryConfig` — `enabled`, `max_messages`,
  `session_key`; embedded in `AgentDefinition` (JSONB snapshot, D1).
- `runtime/agent_runtime.py` — `_load_memory` (get_or_create + FULL
  history), called inside `_run_segment` AFTER model resolution (D28
  site); `_rebuild_messages` reconstructs a resumed segment's context
  from the conversation (FULL history — the documented v1 approximation,
  walkthrough-s6 §11); the loop persists to both the run transcript and
  the conversation.
- `prompt/engine.py` — `PromptContext` carries `history`; `build()`
  appends system + `history_window()` + user; `history_window` slices the
  last `max_messages` when memory is enabled.
- `ports/repository.py:151` `ConversationRepo` — get_or_create / find /
  append_message (gapless per-conversation `sequence`, UNIQUE-backed) /
  history.
- `persistence/models.py` `ConversationRow` / `MessageRow`;
  `SqlConversationRepo.history` orders by `sequence` (gapless 1..N, so a
  message COUNT is a sequence boundary).
- `domain/tools.py:38` `ToolContext` — run_id, agent_id, session_id,
  user_id, variables, config, cancel. No tenant field yet.
- `tools/builtin/` — builtins are `BaseTool` subclasses registered in
  `AppContainer.from_settings` (deps.py); the tools page and agent-editor
  picker render from capabilities' builtins list automatically.
- `models/types.py` `ModelRequest`/`ModelResponse`; the resolved
  `ModelClient` (ports/model.py) exposes non-streaming `generate()` —
  the summarizer call needs no streaming.
- `MockModelProvider` — scripted turns make compaction deterministic in
  unit tests (the compaction call consumes a turn BEFORE the loop's first
  strategy step).
- pgvector: **not installed** on this machine's Postgres (verified:
  `pg_available_extensions` has no `vector` row) — a live constraint on
  the vector item, see §7.

## Decision

### 1. MemoryConfig gains a `strategy`; old snapshots stay valid

    MemoryConfig:
      enabled: bool = False
      max_messages: int = 20          (unchanged)
      session_key: str | None = None  (unchanged)
      strategy: Literal["window", "summarize"] = "window"   (new)

Additive optional-with-default — every existing version snapshot parses
unchanged (D1 discipline; the same backward-compatibility argument as
`RunQueueMessage.kind` in D41). The window behavior is exactly today's,
byte-identical: `strategy="window"` (the default) never calls the
summarizer and never reads summary state.

### 2. Rolling summary lives on the conversation row (D45)

The conversation row gains two columns (migration 0009):

    summary: TEXT NULL            — the rolling summary text
    summarized_count: INTEGER NOT NULL DEFAULT 0
                                 — how many LEADING conversation
                                   messages the summary covers

`summarized_count` is a COUNT, not a sequence number, but because
per-conversation sequences are gapless (UNIQUE-backed `sequence` starting
at 1), count == boundary: `history[:summarized_count]` is exactly the
summarized prefix. No new message role, no enum migration — the summary
is conversation state, not a message. (The roadmap's "a new message role
or a MemoryState row" — this is the MemoryState row, chosen because
`message_role` is a native enum and a synthetic role would leak into
list_messages/replay paths that must stay byte-identical.)

**Compaction** happens at fresh-segment entry, inside `_run_segment`'s
existing try (after client resolve, before the prompt build — the D28
site, so a compaction failure is handled by never-raise rules, not the
worker's claim-failure path):

1. Load the FULL conversation history (as today).
2. If `len(history) - summarized_count > max_messages`: the slice
   `history[summarized_count : len(history) - max_messages]` is
   newly-evictable. Build one summarizer request through the SAME
   resolved client (no second model, no new credential surface): a
   constant system prompt ("You compress conversation history...") +
   the previous summary (if any) + a `role: text` transcript of the
   evicted slice.
3. `client.generate(...)` → new summary text; usage is added to
   `ctx.usage` (the orchestrator owns the budget — the compaction call
   is a model call like any other, ADR 0004).
4. Persist via `ConversationRepo.save_summary(conversation_id, summary,
   summarized_count=old + len(slice))`.
5. The prompt gets: [system] + [summary system-message] + last
   `max_messages` history + [user]. The window itself is unchanged —
   compaction only decides what the window MEANS (recent detail over a
   remembered summary instead of amnesia).

**Failure semantics (a run never raises, D5):** cancellation wins —
`ExecutionCancelled`/`ModelAbortedError` from the summarizer call
propagate to the existing terminal handlers (run.cancelled). Any other
`ModelError` degrades: this segment runs with the window strategy and
whatever summary state already exists, with a logged warning. Compaction
is NEVER a run failure — a memory enhancement must not fail a run that
would have succeeded with the plain window. No event types are added;
memory state is not part of the event envelope (the envelope is frozen,
ADR 0003 — nothing in S12 touches it).

**Ports change (the one, pre-declared here):** `ConversationRepo` gains

    async def get_summary_state(conversation_id) -> ConversationMemoryState | None
    async def save_summary(conversation_id, *, summary: str, summarized_count: int) -> None

`ConversationMemoryState` is a small pure-Pydantic type in
`domain/agent.py` (`summary: str | None`, `summarized_count: int`).
The SQL adapter scopes both by the conversation's tenant when a
tenant_id is supplied (the parent-row check `history()` already uses).
Unit-test fakes (`_RecordingRepo` in test_agent_runtime.py and friends)
grow the two methods.

**Resume:** `_rebuild_messages` (resumed segments, S10/S6 paths) today
rebuilds from the FULL conversation — the documented v1 approximation.
S12 fixes it to the same view a fresh segment sees: existing summary
(if any) + last `max_messages` — read-only, NO compaction on resume
(a resumed segment must not spend tokens before re-entering its paused
iteration). This is a behavior change only for conversations longer
than the window; the golden suites (short histories) are unaffected,
and the change is the honest budget-consistency fix (a resumed segment
previously saw MORE context than a fresh run on the same conversation).

**Workflow agent nodes:** unchanged semantics — node agents are
memory-less in v1 (ADR 0015 §2, publish-time lint); a memory-enabled
node agent ignores memory exactly as today, compaction included.

### 3. Working memory: builtin tools over a per-session KV store (D46)

A new port (new Protocol, not a change to an existing one — pre-declared
here per rule 7), `ports/repository.py`:

    class ScratchpadRepo(Protocol):
        async def get(agent_id, session_id, key, *, tenant_id=None) -> ScratchpadEntry | None
        async def put(agent_id, session_id, key, value, *, tenant_id=None) -> None
        async def delete(agent_id, session_id, key, *, tenant_id=None) -> bool

Backed by one table (migration 0010): `memory_scratch`
(`agent_id`, `tenant_id` NOT NULL default 'default', `session_id`,
`key`, `value TEXT`, `updated_at`; UNIQUE(agent_id, session_id, key);
`put` is an upsert). Tenant-scoped at the repo boundary like every
other repo (D29: foreign keys read as absent, never leaked).

Three builtin tools (`tools/builtin/memory.py`), constructed with the
store (the AppContainer wires `SqlScratchpadRepo(sessionmaker)` — the
same registration line as CalculatorTool):

- `memory_get {key}` → the stored value or an honest is_error "not set".
- `memory_put {key, value}` → upsert; value capped at
  `max_value_chars` (default 16,000, overridable per-binding via
  `ToolBinding.config` — config already flows to `ToolContext.config`).
- `memory_delete {key}` → removed or "not set".

**No session_id → honest is_error result** ("scratchpad requires a
session"), never an exception — the recoverable ToolResult path the
tool runtime already owns. The store is keyed by (agent_id, session_id,
key), NOT by memory.enabled — the scratchpad is a tool surface,
orthogonal to conversation memory; a memory-less agent can still bind
it (the roadmap's "persisted like tool executions" — reads/writes are
ordinary tool calls, visible in `tool_executions` rows like any builtin).

**Domain additive change (pre-declared):** `ToolContext` gains
`tenant_id: str | None = None` — the runtime threads `ctx.tenant_id`
into every tool execution (the two `_execute` call sites in
`agent_runtime.py` and the workflow runtime's tool node path). Without
it the scratchpad could not scope by tenant; with it, every future
tenant-aware tool inherits the field. Optional-with-default — every
existing construction stays valid.

### 4. PromptEngine: one new optional context field

`PromptContext.memory_summary: str | None = None`; `build()` inserts one
system message ("Summary of the earlier conversation: ...") between the
system section and the history window when set. `prompt/` is internal
(not ports/) — no ADR-amendment needed beyond this one; the field is
additive and the window path is untouched (summary=None everywhere
today).

### 5. What S12 does NOT touch

- The event envelope, event types, terminal semantics — nothing in
  `domain/events.py` changes.
- `ports/queue.py`, the worker, the sweeper — compaction happens inside
  the segment; lease/heartbeat semantics are unchanged (a compaction call
  is just part of a run like a model call).
- `RunResult`, the executions surface, SSE — memory is invisible to the
  event stream by design.
- Agent API routes — `MemoryConfig` is embedded in AgentDefinition; the
  new field flows through create/update/versions with zero route changes
  (the editor gains a select, §6).

### 6. API surface and UI enablement

- No new routes. `GET /v1/capabilities` needs NO flip — the scratchpad
  tools appear in the existing `tools.detail.builtins` list (derived
  from the registry) automatically, and memory is an agent-editor
  concern.
- UI: the agent editor's memory panel gains a **Strategy** select
  (window / summarize) shown when memory is enabled; the generated
  OpenAPI client is regenerated (the MemoryConfig schema gained a
  field). The Agents detail page's memory line names the strategy.
- The scratchpad needs no dedicated UI: the tools page lists the new
  builtins; the agent-editor tool picker offers them like calculator.

### 7. Deliberately out of v1 (recorded, not silently dropped)

- **Vector memory — DEFERRED to S8 (D47).** Two live blockers, both
  verified: (a) the pgvector extension is not installed on this
  machine's Postgres (`pg_available_extensions` has no `vector`), so
  neither the store nor its tests could run here; (b) the
  embedding-provider seam (which model embeds, dimension pinning,
  distance metric, tenancy of the index) needs its own design that S8's
  retriever shares — building it inside S12 would guess at S8's
  requirements. The `MemoryStore` port the roadmap sketched is NOT
  added speculatively; `MemoryConfig.strategy` stays
  `window | summarize` and gains `vector` when S8 lands (an additive,
  snapshot-compatible change, same argument as §1). Design note for S8:
  the store is tenant-scoped, embeddings resolved per segment through
  the model factory (the D28 pattern), recall injected through
  `PromptContext` exactly like the summary — the seams S12 builds are
  the ones S8 rides.
- **Cross-session memory** (remembering across conversations) — the
  scratchpad is per (agent, session) by definition; cross-session
  recall is the vector item's problem, deferred with it.
- **Summarizer prompt customization** (`MemoryConfig` params for the
  compaction prompt) — a constant prompt keeps v1 deterministic and
  testable; params arrive if real usage asks for them.
- **Conversation pruning** (deleting old message rows after compaction)
  — rows are the replay source of truth for past runs'
  `_rebuild_messages`; pruning would break resume fidelity. Revisit
  only with a retention story that accounts for it.

## What stays frozen

- The event envelope and every event type (ADR 0003) — S12 adds no
  events, no envelope fields.
- Terminal semantics, the error shape, the queue contract, the worker —
  untouched.
- `ConversationRepo`'s existing four methods and their signatures (the
  two new methods are additions, not modifications).
- Window-strategy behavior — byte-identical (the golden suites are the
  gate).
- Secret rules — no new credential surface (the summarizer rides the
  agent's already-resolved client).

## Consequences

- A summarize-strategy run spends tokens on compaction before its first
  iteration; usage and the token budget (RunLimits) account for it
  because the call rides `ctx.usage`.
- The conversation row becomes stateful beyond messages (summary state);
  it is still the single source of conversation truth — no second store.
- Resumed segments on long conversations see strictly less context than
  before (the window, not the full history) — the fix of the documented
  S6 v1 approximation, gated by the resume-flow regression tests.
- The `_RecordingRepo` fakes in unit tests grow the two summary methods
  (in-memory dict) — every runtime unit test that touches memory stays
  hermetic.
- `ToolContext.tenant_id` is available to all tools; only the scratchpad
  uses it in S12, but MCP/future tools may follow.