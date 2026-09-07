# Writing strategy plugins for JARVIS

A strategy plugin is an ordinary Python package that teaches JARVIS a new
agent loop — plan-and-execute, tree-of-thought, reflexion, whatever —
without touching a line of the core. This doc is the contract third-party
authors build against. Everything in it is a restatement of ADR 0004 (the
orchestrator owns limits; a strategy owns one step) and decision D5 (a run
never raises) written for an audience outside this repo.

## What a strategy is

`AgentStrategy` is a structural Protocol (from `jarvis.ports.strategy`):

```python
class AgentStrategy(Protocol):
    @property
    def name(self) -> str: ...

    async def step(
        self,
        ctx: ExecutionContext,
        messages: list[Message],
        client: ModelClient,
        tools: list[ToolDescriptor],
        sink: EventSink,
    ) -> StepOutcome: ...
```

You subclass nothing — implementing the two members is enough
(`@runtime_checkable`). Your package depends on `jarvis`; jarvis never
depends on your package except through the entry point.

`StepOutcome` has three variants, discriminated by `kind`:

- **`ToolCallsStep`** — the assistant message plus tool calls for the
  orchestrator to execute. An **empty `tool_calls` list is a "think"
  step**: the assistant message is persisted and the loop continues. That
  is how multi-phase strategies (plan → execute) emit intermediate
  reasoning without ending the run.
- **`FinishStep`** — the run completes with `finish_reason`.
- **`AskHumanStep`** — the run pauses as `awaiting_input` (ADR 0010); the
  human's answer arrives as a resume content message.

The orchestrator appends your `messages` (e.g. a format-correction
assistant/user pair) after the assistant message.

## The rules (ADR 0004, restated)

1. **At most ONE model invocation per `step` call.** `step` is one phase,
   not the loop.
2. **Never loop, never recurse.** The orchestrator owns iteration caps,
   token budget, and deadline. If the transcript needs another round, just
   return — it will call you again.
3. **Never emit terminal events.** Only `AgentRuntime.run()` may
   `finalize()`. Stream partial output (`text.delta`, model events)
   through the `sink` you are handed — nothing else.
4. **Don't execute tools.** Return `ToolCallsStep`; the orchestrator runs
   them (and owns approvals, cancellation, and results).
5. **Fail honestly.** Raising in `step` is allowed — the run terminal-fails
   as a persisted `run.failed` with `error_kind="strategy"`. Do not
   catch-and-fake a success.

## Stateless by construction

The registry holds **one shared instance per strategy** and reuses it
across every concurrent run. Derive per-run state from `messages` and
`ctx` (the pattern ReAct itself uses — e.g. "is the plan marker present in
an assistant message yet?"), never from instance attributes. Construction
per resolve for stateful strategies is a deliberate deferral, not a
license for mutable plugin state.

## Configuration

Agents configure your strategy as

```yaml
strategy:
  type: plan_execute        # your entry-point name
  params:                   # free-form dict, flows verbatim
    plan_marker: "PLAN:"
```

`params` reaches `step()` as `ctx.metadata["strategy_params"]` — the frozen
Protocol takes no config argument, so the orchestrator stamps the ctx before
the loop (the same pattern as `ctx.output_schema`). It is persisted in the
immutable agent version snapshot. It is therefore **never a place for
secrets** — put credential references there (the platform's job, ADR
0005/0006), never key material.

## Packaging and entry points

Ship a normal Python distribution with one entry-point group,
`jarvis.strategies`. The entry-point **name is the strategy id** agents
write in `strategy.type` (snake_case); the **value is `module:Attr`**
pointing at an instance or a zero-arg callable class:

```toml
# pyproject.toml
[project]
name = "my-jarvis-strategies"
version = "0.1.0"
dependencies = ["jarvis"]          # types only — never the reverse

[project.entry-points."jarvis.strategies"]
plan_execute = "my_jarvis_strategies.plan_execute:PlanExecuteStrategy"
```

Install with `pip install` / `uv pip install` (or a path dep in dev), set
the allow-list, restart:

```bash
export JARVIS_STRATEGY_PLUGIN_ALLOWLIST=plan_execute
# restart the API (and every worker)
```

Loading is gated by that allow-list — an installed plugin not named there
is skipped silently, so a dependency can ship strategies without
activating them. There is **no hot load**: a restart moves the
installed/allow-listed set.

Degenerate cases you'll see in `GET /v1/capabilities` (section
`plugins.detail`): an allow-listed name with no installed entry point is
reported as `missing`; a plugin that raises on import is reported with its
error and skipped — the rest still load and the server boots either way.

## What's stable

`jarvis.ports.strategy` (`AgentStrategy`, `StepOutcome` and its three
variants) and `jarvis.domain` (`Message`, `ExecutionContext`, …) change
only via an ADR — the same guarantee core code gets. There is no semver
gate on jarvis imports yet; the ADR rule is the only stability promise.

## One caveat worth knowing

Validation of `strategy.type` happens at **create time against the
allow-list of the process doing the creating**. A headless CLI
(`jarvis agent create`) run with a different `JARVIS_STRATEGY_PLUGIN_ALLOWLIST`
than the API server can accept a type the server can't resolve; the
version still runs — to a persisted `strategy` failure, not a crash
(D36). Keep the two processes' allow-lists consistent. The same applies to
**workers**: any worker that claims the run resolves the strategy against
its own allow-list, so in a distributed deployment every worker should
carry the same allow-list as the API.