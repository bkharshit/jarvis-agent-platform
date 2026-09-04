# Runtime Semantics

Authority: ADR 0004, `docs/decisions.md` D5–D12, `CLAUDE.md` rules 2–3
and 5.

## The contract

- **A run never raises.** Every failure resolves to a persisted terminal
  state with an `error_kind` (max_iterations | timeout | model | tool |
  output_schema). No route, worker, or caller needs try/except around
  `AgentRuntime.run()`.
- **Orchestrator owns limits**; a strategy performs at most one model
  invocation per `step()` and returns `ToolCallsStep` or `FinishStep`.
  Strategies must never loop, never raise past the runtime, and emit
  deltas only through the sink. Terminal events belong to the orchestrator.
- **Cancellation is cooperative** (D6): `ctx.cancel` is checked at defined
  checkpoints; tools must not be asyncio-cancelled mid-flight. New
  cancellation checks belong at loop boundaries, not inside tool bodies.
- **Iterations are double-clamped** (D7): per-agent `max_iterations` at the
  loop bottom *and* platform `RunLimits` (budget, deadline) at the top — a
  hostile agent definition cannot escape the platform cap. Any new loop
  path must go through both.
- **Tools implement `_execute` only** (D10): validation, timeout,
  cancellation guard, truncation, and exception→error-result live in
  `ToolRuntime`. A tool that can crash a run is a defect in the runtime or
  the tool — flag either.
- **Fail-closed tools** (D11): network tools (e.g. `http_get`) refuse
  everything without an explicit allow-list.
- **Secrets by reference** (ADR 0005, D18): only env-var *names* in config
  (`api_key_env`); never a literal key, never a key in `ToolContext`
  config, never a key in logs.

## What to flag

- `try/except` around `runtime.run()` in callers; exceptions escaping the
  runtime; strategies looping or emitting terminal events.
- New limit/budget checks that bypass `RunLimits`, or a code path that can
  exceed `max_iterations`.
- A tool doing its own validation/timeout instead of relying on the
  template-method runtime; a tool importing asyncio cancellation.
- Allow-lists defaulting open; `*` hosts outside tests.
- Retry logic outside the model adapter's narrow rule (RateLimit +
  Connection only, max 2).

## Severity calibration

- A run that can raise, hang (no deadline), or double-emit terminal:
  **P0**. A strategy that loops or a tool that can crash a run: **P1**.
- Missing platform clamp or fail-open network tool: **P1** (security/
  integrity boundary).