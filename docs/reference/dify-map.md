# Dify Reference Map (verified paths)

Reference: Dify clone at `./dify-reference` (read-only, HEAD Sept 2026).
Architectural reference only — no code copying.

Critical context: that checkout is **mid-migration** — the agent runtime was
extracted into a separate `dify-agent/` service (loop delegated to
pydantic-ai), the workflow engine/model runtime moved into an external
`graphon==0.7.0` pip package, a Go `dify-agent-runtime/` handles sandboxed
shell, and legacy ReAct/FC runners survive in `api/core/agent/`. The repo
shows both "before" (in-process loop) and "after" (external service) — and the
migration cost itself is the lesson: get the seams right early.

| Our subsystem | Dify reference (verified) | Teaches |
|---|---|---|
| Agent loop | `dify-agent/src/dify_agent/runtime/{runner,run_scheduler,agent_factory}.py`; legacy `api/core/agent/{cot,fc}_agent_runner.py` | Process-local scheduler + asyncio supervisor; explicit loop with max-iterations and forced-final-answer on last iteration; terminal events committed atomically (exactly one wins) |
| Agent strategies | `api/core/agent/strategy/base.py`; new `dify-agent/src/agenton/layers/base.py` + `compositor/providers.py` | Strategy must be a genuine extension point; Dify evolved from enum+subclass to compositional registry keyed by `type_id` |
| Model abstraction | graphon model_runtime (external); `api/core/model_manager.py`; `api/core/plugin/impl/model_runtime.py` | Separate generate/stream (avoid Dify's triple-overloaded `invoke_llm(stream=...)` union); capability flags; typed error mapping |
| Tool system | `api/core/tools/__base/{tool,tool_provider,tool_runtime}.py`; five families in `api/core/tools/` | Template-method `Tool.invoke()` (validation/normalization public, `_invoke()` abstract) + message factories; AVOID god-class `tool_manager.py` (~1200 lines) |
| MCP (Phase 4) | `api/core/mcp/mcp_client.py`, `api/core/tools/mcp_tool/` | MCP = just another Tool family behind the same base class (our Rule 4) |
| Prompt engine | `api/core/prompt/{prompt_transform,advanced_prompt_transform,agent_history_prompt_transform}.py` | Prompt building as a transform fed a context object; token-budgeted history; `{{var}}` templates |
| Execution events | `dify-agent/src/dify_agent/protocol/schemas.py`; `runtime/event_sink.py`; `storage/redis_run_store.py` | Type-discriminated event union; terminal event = status transition; event-sink port with cursor replay (Last-Event-ID); delta coalescing |
| State/memory | `dify-agent/src/agenton/compositor/schemas.py`; `runtime/history.py` | Serializable session snapshots for resume; history as reserved concern |
| Workflow engine (Phase 5) | graphon `GraphEngine` + `GraphEngineLayer` middleware; `api/core/workflow/{workflow_entry,node_factory,node_runtime}.py`, `nodes/` | Protocol-injection seam (host capabilities wired into engine-generic nodes); persistence/observability/limits as engine layers; nodes return `NodeRunResult` OR yield events; versioned node classes |
| Persistence | `api/models/workflow.py`, `api/models/agent.py`, `api/models/model.py` | Snapshot definition into each run (immutable history + replay); step-execution rows with inputs/outputs/status/error/elapsed; AVOID LongText-JSON columns (we use typed JSONB) |
| API/streaming | `api/controllers/console/app/workflow.py`; `api/core/app/apps/base_app_generator.py::convert_to_event_stream`; `api/core/app/entities/queue_entities.py` | Thin controllers → services; typed event queue → per-event handlers → SSE framing in ONE place; durable event log + replay endpoint |
| Frontend builder (Phase 2) | `web/app/components/workflow/` (ReactFlow; NodeComponentMap keyed by BlockEnum; zustand slices + zundo); `hooks/use-nodes-sync-draft.ts` | Frontend registry mirrors backend node types; `_`-prefixed runtime-state convention stripped at save; draft-save with server hash for optimistic concurrency |
| Plugins (Phase 12) | `api/core/plugin/impl/*`, `plugin_service.py` | All external variety flows through one client boundary and surfaces via the same Tool/AIModel base abstractions |