# S9 implementation plan — small hardening items (grab-bag)

Roadmap §S9 lists four independent items. This plan is the build-time
sequence (one commit per step, gates green before every commit), in the
style of the Phase 1 plan.

**Rides on:** existing seams only — the tool-execution batch inside
`AgentRuntime._loop`, the prompt engine's closed renderer,
`ports/events.py`, and the integration conftest's self-bootstrap. No new
ports, no new adapters beyond a behavior flip.

## Scope decided up front

1. **Parallel tool calls** — `ModelCapabilities.parallel_tool_calls`
   (models/capabilities.py, default False) is declared but never consumed;
   the runtime executes a batch strictly sequentially. Flip: per-agent
   opt-in (`AgentDefinition.parallel_tool_calls`) ANDed with the provider
   capability, exactly like `structured_mode` already combines definition +
   capabilities. (D78)
2. **Jinja2 sandboxing** — jinja2 was never adopted: the prompt engine
   renders user-supplied templates (`system_prompt`,
   `user_prompt_template`) with a closed regex `{{var}}` substitutor —
   inherently inert, no eval, no Environment. The hardening item completes
   as a recorded decision + regression tests proving hostile template
   syntax stays literal; adopting jinja2 later requires
   `SandboxedEnvironment` by decision. (D79)
3. **Redis event bus** — the roadmap's own condition ("only if S1's
   Postgres fan-out proves insufficient") never fired. Recorded deferral
   with revisit conditions. (D80)
4. **Integration tests without Docker** — the conftest already
   self-bootstraps `jarvis_test` (ADMIN_URL CREATE DATABASE + self-migrate
   + truncate); README documents the path. Remaining work: assert the
   path in-repo (a no-Docker smoke entry) and align `make test-db` docs.
   No Docker on this machine — `make test-db` itself is never used here.

## Commit sequence

| # | Commit | Contents | Gates |
| --- | --- | --- | --- |
| 1 | `feat(s9): parallel tool execution — per-agent opt-in behind the provider capability` | `AgentDefinition.parallel_tool_calls: bool = False` (domain, additive — existing snapshots stay valid); `_loop` gathers concurrent `tool_runtime.execute` when the flag AND `client.capabilities.parallel_tool_calls` are both true and the batch has >1 executable call; events stay deterministic (started frames for the whole batch up front, completed/failed frames and tool messages appended in call order); per-call tool.call spans unchanged; handoff/approval/decline paths untouched. Unit tests: sequential default (byte-order identical), parallel batches preserve call-order messages/events, capability gate off = sequential. | unit, lint, typecheck |
| 2 | `test(s9): the prompt template renderer is a closed sandbox (D79)` | Decision note + unit tests: `{{var}}` substitution, hostile jinja (`{% raw %}`, `{{ x.__class__ }}`, filters/pipes, `{%%}` blocks) passes through inertly, unknown vars render empty per the existing rule. | unit, lint, typecheck |
| 3 | `docs(s9): defer the Redis bus (D80); Docker-free integration path documented` | decisions.md D80; README integration-test section gains the exact no-Docker recipe (local Postgres role/db, `uv run pytest tests/integration -m db`); roadmap S9 marked shipped. | — |

## Verified seams

- `src/jarvis/models/capabilities.py:22` — `parallel_tool_calls: bool = False`,
  declared on every provider client (`client.capabilities`), consumed only
  for `structured_output` today.
- `src/jarvis/runtime/agent_runtime.py:704` — the sequential
  `for call in calls:` batch; started frames + refusals already precede
  execution; handoff and `_fold_usage` ride per-call results.
- `src/jarvis/runtime/agent_runtime.py:382` — the definition × capability
  combination precedent (`ctx.structured_mode`).
- `src/jarvis/prompt/engine.py:112` — `render()` is a regex substitution
  (`_VAR_PATTERN`), the only renderer of user-supplied templates.
- `tests/integration/conftest.py:47` — `_create_test_db()` over
  `ADMIN_URL`; self-migrating, truncating fixtures.

## Acceptance

- Default agents run byte-identically (golden unit suite unchanged).
- A parallel-opted agent on a capability-declaring provider executes a
  multi-call batch concurrently, with event/message order still
  call-ordered and spans correct per call.
- Hostile templates can never execute code — regression-tested.
- The Docker-free integration recipe is in the README and matches what
  the conftest actually does.