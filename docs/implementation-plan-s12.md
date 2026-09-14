# Implementation plan — S12: richer memory (ADR 0016)

Stage: S12. Format follows `docs/implementation-plan-s6.md`. Every commit
below is independently green: `pytest tests/unit`, `ruff check src
tests` + format, `mypy src` — and per web commit `tsc --noEmit`,
eslint, `vitest`. Integration commits additionally pass
`uv run pytest tests/integration -q -m db` (local Postgres, no Docker).

Scope (ADR 0016): summarization/compaction + scratchpad. Vector memory
is deferred to S8 (D47 — pgvector not installed locally; the embedding
seam is S8's to design).

## Verified seams (read in code, 2026-09-14)

- `domain/agent.py:51` `MemoryConfig` (`enabled`, `max_messages`,
  `session_key`) — embedded in `AgentDefinition`, JSONB snapshot (D1).
- `runtime/agent_runtime.py:965` `_load_memory` (get_or_create + FULL
  history, called in `_run_segment` after client resolve — the D28 site);
  `:925` `_rebuild_messages` (resume path, FULL conversation history —
  the documented v1 approximation); `:594` the ToolContext construction
  in the loop; `workflow_runtime.py` tool-node ToolContext sites.
- `prompt/engine.py:22` `PromptContext`; `:74` `history_window`;
  `:40` `build()` (system + window + user).
- `ports/repository.py:151` `ConversationRepo` (get_or_create / find /
  append_message / history); `persistence/repositories.py:1067`
  `SqlConversationRepo` (history orders by gapless `sequence`; the
  tenant parent-row check pattern at `:1135`).
- `domain/tools.py:38` `ToolContext` (no tenant field);
  `tools/builtin/calculator.py` (BaseTool pattern);
  `api/deps.py:93` builtin registration in `AppContainer.from_settings`.
- `persistence/migrations/versions/` — 0001..0008; next is 0009.
- `models/mock.py` `MockModelProvider` scripted turns (the compaction
  call consumes a turn BEFORE the loop's first strategy step —
  deterministic tests).
- Unit fakes: `tests/unit/test_agent_runtime.py:50` `_RecordingRepo`
  doubles both repos (grows the two summary methods).
- Web: `web/src/sections/agents/AgentEditor.tsx:577` memory panel;
  `web/src/stores/editorStore.ts:38` memory draft shape;
  `AgentDetailPage.tsx:57` memory line.
- pgvector NOT available locally (verified `pg_available_extensions`).

## Confirmed decisions (ADR 0016 carries the contract)

### D45 — rolling conversation summary on the conversation row

`MemoryConfig.strategy: Literal["window", "summarize"] = "window"`
(additive; old snapshots parse unchanged). The conversation row gains
`summary TEXT NULL` + `summarized_count INTEGER NOT NULL DEFAULT 0`
(count == gapless-sequence boundary). Compaction at fresh-segment entry
inside the segment's try: evicted slice → ONE summarizer call through
the SAME resolved client, usage into `ctx.usage`, state saved through
two NEW `ConversationRepo` methods (`get_summary_state` /
`save_summary` — the one ports/ change, pre-declared ADR 0016 §2).
Failure rules: cancellation propagates; other ModelError degrades to the
window for that segment (logged) — compaction can never fail a run. No
events, no envelope changes. Resume rebuild applies summary + window
read-only (fixes the S6 v1 full-history approximation).

### D46 — scratchpad: builtin tools over a per-session tenant-scoped store

New `ScratchpadRepo` port (get/put/delete keyed agent_id + session_id +
key, tenant-scoped) + `memory_scratch` table (UNIQUE(agent_id,
session_id, key), upsert on put). Builtins `memory_get` / `memory_put` /
`memory_delete` constructed with the store; value cap
`max_value_chars` (default 16,000) overridable per binding via config;
no session_id → honest is_error result. `ToolContext` gains optional
`tenant_id` (threaded by the runtime — additive domain change,
pre-declared ADR 0016 §3).

### D47 — vector memory deferred to S8

pgvector is not installed on this machine and the embedding-provider
seam (model, dimensions, metric, tenancy) is S8's to design with its
retriever. No speculative `MemoryStore` port; `strategy` gains
`vector` when S8 lands (additive, snapshot-compatible). Design note
recorded (ADR 0016 §7): tenant-scoped store, per-segment model-factory
resolution (D28 pattern), recall injected through `PromptContext` like
the summary.

## Commit sequence

1. **Planning** (this commit): ADR 0016 + this plan + D45–D47 in
   decisions.md + roadmap S12 planning record. No S12 code.
2. **Domain + prompt**: `MemoryConfig.strategy`;
   `ConversationMemoryState` (domain/agent.py);
   `PromptContext.memory_summary` + engine injection (prompt/);
   `ToolContext.tenant_id` (domain/tools.py). Unit: prompt-engine
   summary placement, domain-model parse of old snapshots.
3. **Conversation summary state**: the two `ConversationRepo` port
   methods + `SqlConversationRepo` impl (tenant parent-row check) +
   migration 0009 (`conversations.summary`, `summarized_count`) +
   `_RecordingRepo` fakes + integration tests (save/get round-trip,
   tenant scoping).
4. **Runtime compaction**: `runtime/memory.py` (load window+summary,
   compact via the resolved client, degrade on ModelError);
   `_run_segment` integration; `_rebuild_messages` windowing; loop
   ToolContext gains tenant_id threading. Unit: compaction consumes a
   scripted turn and saves state; degrade path; cancel path; resume
   windowing; the full golden suite stays green (window default =
   byte-identical).
5. **Scratchpad store**: `ScratchpadEntry` (domain) + `ScratchpadRepo`
   port + `SqlScratchpadRepo` + migration 0010 (`memory_scratch`) +
   integration tests (upsert, tenant scoping, delete).
6. **Scratchpad tools**: `tools/builtin/memory.py`
   (MemoryGetTool/MemoryPutTool/MemoryDeleteTool) + AppContainer
   registration + unit tests (in-memory store fake, no-session error,
   value cap, binding-config override).
7. **Web**: gen-api regen (MemoryConfig.strategy in the client);
   agent-editor memory Strategy select (window/summarize, shown when
   memory enabled); editorStore draft + save payload; Agents detail
   memory line names the strategy; vitest.
8. **Manual session + closure** (post-build, Harshit): run
   `docs/walkthrough-s12.md` live together; then the closure commit —
   walkthrough with session notes, README memory section, roadmap S12
   shipped note, CLAUDE.md gotchas from the build.

## Rule-7 audit (what touches what)

| Touch | File(s) | Frozen-surface? |
| --- | --- | --- |
| `MemoryConfig.strategy` | `domain/agent.py` | additive field, default preserves every snapshot |
| `ConversationMemoryState` (new type) | `domain/agent.py` | new, pure Pydantic |
| `PromptContext.memory_summary` + injection | `prompt/engine.py` | internal (not ports/); additive |
| `ToolContext.tenant_id` | `domain/tools.py` | additive optional, pre-declared ADR 0016 §3 |
| `ConversationRepo.get_summary_state` / `save_summary` | `ports/repository.py` | THE ports/ change — new methods on an existing Protocol, pre-declared ADR 0016 §2 |
| `ScratchpadRepo` (new Protocol) | `ports/repository.py` | new Protocol, not a change to an existing one |
| migration 0009 / 0010 | `persistence/migrations/versions/` | additive columns / new table |
| `_load_memory` → memory loader, `_rebuild_messages` | `runtime/agent_runtime.py` | window default byte-identical (golden gate) |
| ToolContext construction sites | `agent_runtime.py`, `workflow_runtime.py` | additive kwarg |
| builtins registration | `api/deps.py` | one line per new tool |
| capabilities | none | no flip — builtins list is derived |

## Acceptance (roadmap S12, adapted to the shipped scope)

- A long conversation with `strategy=summarize` answers a question that
  requires the FIRST turn's content while the prompt stays within the
  window (the window strategy provably fails this; the summarizer
  passes) — unit-proven with the mock provider, live-proven on
  gemma4:31b in the manual session.
- A summarize run whose summarizer call fails (mock error injection)
  still completes with the window strategy — the run never fails on
  memory.
- `strategy=window` (and every pre-S12 agent) — byte-identical event
  sequences (golden suite).
- An agent binds `memory_put`/`memory_get`, writes in one run, reads
  back in another run of the same session; a different session/tenant
  does not see the value; a run without a session gets an honest error.
- Resume on a long conversation uses summary + window, not full history.