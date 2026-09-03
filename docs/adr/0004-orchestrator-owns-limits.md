# ADR 0004 — Orchestrator owns limits; strategy owns one step

- **Status**: Accepted
- **Date**: 2026-09-04

## Context

In Dify, loop control historically lived inside agent runner classes
(`api/core/agent/cot_agent_runner.py`), mixing iteration policy, prompt
building, tool dispatch, and event emission into one class. Strategies should
be a genuine extension point (Dify's evolution from enum+subclass to a
`type_id` registry shows the need), but limits, cancellation, budget, and
terminal-event semantics are runtime concerns — a misbehaving strategy must
not be able to loop forever or emit two terminal events.

## Decision

- `AgentStrategy.step(ctx, messages, client, tools, sink) -> StepOutcome`
  performs **at most one model invocation** and returns `ToolCallsStep` or
  `FinishStep`. It never loops and never emits terminal events (it may stream
  `text.delta` / `model.*` via the sink).
- `AgentRuntime.run()` owns the loop: iteration cap, deadline, token budget,
  cancellation, tool execution, memory, persistence, and terminal event
  emission (`sink.finalize()` exactly once).
- Blocking `/run` and streaming `/stream` call the **same**
  `AgentRuntime.run()`; the SSE route subscribes to the sink while the run
  executes as an asyncio task. Identical event sequences are guarded by test.

## Consequences

- ReAct, function calling, and future custom strategies are first-class and
  small.
- Runaway strategies are structurally impossible: the orchestrator clamps the
  loop (`max_iterations` 1–32).
- Failure classification (`error_kind`: max_iterations | timeout | model |
  tool | output_schema) is centralized and consistent.