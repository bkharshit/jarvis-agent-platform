# JARVIS — Implementation Plan (Stage S3: plugin strategies)

- **Status**: Planned (this doc precedes the build session; no S3 code exists yet)
- **Date**: 2026-09-07
- **Rides on**: ADR 0004 (a strategy owns one step; the orchestrator owns
  the loop, limits, and terminal events — the plugin contract is ADR 0004
  restated for third parties), ADR 0002 (typed JSONB snapshots — widened
  `strategy.type` is a superset, old snapshots stay valid), ADR 0003 (the
  envelope shape is frozen; `error_kind` is a free string — precedent:
  `"output_schema"`, `"timeout"`, `"max_iterations"`), D5 (a run never
  raises — a bad plugin is a persisted terminal state), roadmap §S3.
- **No new ADR** (rule-7 audit in §7): nothing in `ports/`, the event
  envelope, or the error shape changes. The stage records **D35 + D36**
  in `docs/decisions.md` and ships the plugin contract doc the roadmap
  calls for (`docs/plugins/strategy-plugins.md`).

## Context

Only two loop strategies exist (`function_calling`, `react`), and the set
is closed twice: once in the domain model (`StrategyConfig.type` is a
`Literal`) and once in the registry (`_BUILTINS` in
`src/jarvis/strategies/registry.py:10`). S3 opens the second without
losing the first's safety: third-party strategies (plan-and-execute,
tree-of-thought, …) install as **packages**, are discovered through
**entry-points**, and load **only when named in a Settings allow-list**.
The core never changes to add one.

## Verified seams (read in code, 2026-09-07)

- `ports/strategy.py` — `AgentStrategy` Protocol (`name`, one `step`
  call → `StepOutcome`), `StepOutcome` discriminated by `kind`
  (`tool_calls` | `finish` | `ask_human`), `StrategyRegistry` Protocol
  whose docstring already pins "Raises on unknown strategy type".
- `strategies/registry.py` — `DefaultStrategyRegistry` already accepts
  an `extra` dict at construction (the injection point for plugins) and
  already exposes `names()` "feeds /v1/capabilities". Strategies are
  shared singletons; the class docstring anticipates per-resolve
  construction for stateful strategies.
- `domain/agent.py:57` — `StrategyConfig.type:
  Literal["function_calling", "react"]` + free-form `params: dict` (the
  roadmap is right: plugin params need no model change).
- `runtime/agent_runtime.py:339` — `self._strategies.resolve(agent.strategy)`
  inside `_loop`; `run()`'s try (`agent_runtime.py:159`) already
  blanket-catches `Exception` → persisted `run.failed`, so an unknown
  strategy **today** degrades to a persisted `model`-kind failure (wrong
  label, right never-raise behavior — S3 gives it its own kind).
- `runtime/agent_runtime.py:387-410` — a `ToolCallsStep` with an empty
  `tool_calls` list persists the assistant message, runs no tools, and
  **continues the loop**: a stateless multi-phase plugin can emit
  "think" steps (plan phase) without ending the run — the same
  transcript-driven discipline ReAct uses.
- `api/routes/capabilities.py:52` — the `plugins` section flag is
  staged (`{"enabled": False, "stage": "S3"}`); `build_capabilities`
  already derives the agents detail from
  `container.strategies.names()`, so plugin names reach the agent
  editor's strategy picker with **zero web changes** (`AgentEditor.tsx`
  renders options from capabilities, `web/src/sections/agents/AgentEditor.tsx:104`).
- `api/deps.py:102` — `DefaultStrategyRegistry()` is constructed in
  `AppContainer.from_settings`: discovery has exactly one wiring point,
  and the worker shares the same container (a plugin strategy runs on
  any worker, no extra plumbing).
- `config.py` — pydantic-settings, `JARVIS_` env prefix; no list-valued
  setting exists yet (CSV parsing is new ground — see D35).
- `cli/main.py:245` — `jarvis agent create --file` goes **direct to the
  repo**, bypassing API validation: create-time strategy validation must
  be added on both paths or YAML typos surface only at run time.
- pyproject — no `[dependency-groups]`; dev deps ride
  `[project.optional-dependencies] dev` + `uv sync --extra dev` (the
  fixture distribution hooks in here, §5).

## Confirmed decisions (to be recorded as D35/D36 in commit 1)

### D35 — Plugins load from installed entry-points behind a Settings allow-list

Third-party strategies are **packages**. Discovery reads
`importlib.metadata.entry_points(group="jarvis.strategies")` once at
container build; an entry point loads **only if its name is in
`Settings.strategy_plugin_allowlist`** (env
`JARVIS_STRATEGY_PLUGIN_ALLOWLIST`, comma-separated; empty default —
nothing loads unless opted in). The filesystem is never scanned. Three
degenerate cases are facts the API reports, never boot crashes:

- allow-listed name with no installed entry point → recorded as
  `missing`, listed in capabilities;
- allow-listed plugin that raises on import → recorded with its error,
  skipped, the rest still load;
- installed but not allow-listed → skipped silently (opt-in means
  opt-in).

Install/uninstall is `pip|uv install` + restart; there is no hot load.

### D36 — `StrategyConfig.type` widens to `str`; the registry boundary owns validation

The domain Literal becomes `str` (a superset — every existing snapshot
and YAML stays valid; no migration). Typo protection moves to the
**create boundary**: API create/update validates `strategy.type` against
the live registry (422 naming the known strategies, same envelope shape
pydantic already produced); `jarvis agent create` does the same with a
friendly CLI error (D23 pattern). The **resolve boundary** stays
authoritative for already-pinned versions: a snapshot whose strategy is
gone (plugin uninstalled or de-allow-listed) fails as a persisted
terminal `run.failed` with a new `error_kind="strategy"` — never an
exception past the runtime, never a worker retry loop. A plugin whose
`step` raises is caught at the step call site and maps to the same
`error_kind="strategy"` (ExecutionCancelled / ModelAbortedError /
ModelError re-raise first — the outer handlers own those). Envelope
shape unchanged (ADR 0003); adding an `error_kind` *value* follows the
existing precedent, no ADR.

## Design

### 1. Loader — `src/jarvis/strategies/plugins.py` (new)

```
load_strategy_plugins(allowlist: list[str],
                      entry_points: Iterable[EntryPoint] | None = None,
                      ) -> PluginLoadResult
```

- `PluginLoadResult` (pydantic): `loaded: list[StrategyPluginInfo]`,
  `failed: list[StrategyPluginInfo]` (import error string),
  `missing: list[str]` (allow-listed, not installed).
- `entry_points=None` → real `importlib.metadata` discovery; unit tests
  pass stdlib-constructed `EntryPoint(name, value, group)` objects —
  `EntryPoint.load()` imports `module:attr` and needs **no installed
  distribution**, so unit tests don't depend on the fixture being
  installed.
- `StrategyPluginInfo`: `{name, distribution, version, origin
  ("plugin"), error?}`. Builtins get mirrored info with
  `origin="builtin"` so capabilities describes one uniform list.
- Pure function; no IO beyond import. Loading a plugin means
  instantiating the entry (shared instance — see contract, §4).

### 2. Registry & container wiring

- `DefaultStrategyRegistry(extra=...)` gains what it needs to *describe*
  itself: a `describe() -> list[StrategyInfo]` returning name, origin,
  distribution, version, error for every known strategy (builtins
  included). `names()` stays (agents detail + editor picker).
- `AppContainer.from_settings` calls the loader with
  `settings.strategy_plugin_allowlist`, passes loaded instances via
  `extra`, keeps the `PluginLoadResult` on the container for
  capabilities. Failures are carried, never raised — `serve` must boot
  with a broken plugin on the allow-list.
- `Settings.strategy_plugin_allowlist: list[str] = []` — a
  `field_validator` parses the CSV env form (`"plan_execute,raise"`
  → `["plan_execute", "raise"]`); the list default keeps
  programmatic/Settings construction clean.

### 3. Runtime failure mapping (`runtime/agent_runtime.py`)

- **Resolve site** (`_loop`, line 339): wrap `resolve()` in
  `try/except KeyError` — the Protocol documents that raise — returning
  `LoopOutcome(kind="failed", error_kind="strategy", error=...)` naming
  the known strategies (the registry's message already does).
- **Step call site** (line 387): `except (ExecutionCancelled,
  ModelAbortedError, ModelError): raise` then `except Exception` →
  `LoopOutcome(kind="failed", error_kind="strategy", error=f"strategy
  '{name}' raised {type}: {exc}")`. This is the only code the runtime
  ever runs on a plugin, so a plugin cannot produce any other failure
  path.
- Both ride `run()`'s existing finalize; exactly one terminal event, no
  worker-visible exception. The blanket `except Exception` in `run()`
  stays as the final net for everything else.

### 4. Plugin contract — `docs/plugins/strategy-plugins.md` (new)

The doc third-party authors read. Pins (all restatements of ADR 0004 /
D5 for an audience outside the repo):

- **Entry point**: group `jarvis.strategies`, name = snake_case strategy
  id, value = `module:Attr` implementing `AgentStrategy` structurally
  (`@runtime_checkable` — no core subclassing needed, but
  `StepOutcome`/`Message` types come from `jarvis.ports.strategy` /
  `jarvis.domain`; a plugin *depends on* jarvis, never the reverse).
- **The rules**: at most ONE model invocation per `step`; never loop;
  never emit terminal events; stream deltas through the sink only;
  return a `StepOutcome` variant — `tool_calls` (empty list = a think
  step; the loop continues), `finish`, `ask_human` (S10 pause).
- **Stateless**: the registry holds ONE shared instance per strategy —
  derive per-run state from `messages`/`ctx` (the ReAct pattern), never
  from instance attributes. Per-resolve construction is explicitly
  deferred.
- **Params**: `StrategyConfig.params` flows verbatim; it is persisted in
  the immutable version snapshot → **never secrets** (credential refs
  are the platform's job, ADR 0005/0006).
- **Raising**: allowed, but the run terminal-fails with
  `error_kind="strategy"` — fail honestly, don't catch-and-fake.
- **Stability**: `ports/strategy.py` + `StepOutcome` change only via ADR
  (the same guarantee core code gets); no version pin mechanism yet
  (§9).
- **Install**: it's a package + an env var + a restart; include a
  minimal `pyproject.toml` entry-point example.

### 5. Fixture distribution — `tests/fixtures/strategies/jarvis-strategy-fixtures/`

A real installable package proving the acceptance line ("a sample
plugin strategy runs an agent through the API with zero core changes"):

- Own `pyproject.toml`, dist name `jarvis-strategy-fixtures`, one
  entry point group `jarvis.strategies` with **two** entries:
  - `plan_execute` → `PlanExecuteStrategy` — the sample. Stateless,
    phase inferred from transcript markers: no `PLAN:` in any assistant
    message → one call asking for a numbered plan, returned as a
    think step (empty `tool_calls`); plan present → one call executing
    the next step (tool calls or text); `DONE:` prefix → `FinishStep`.
    Markers configurable via `StrategyConfig.params` (exercises the
    params flow).
  - `raise_plugin` → `RaisePluginStrategy` — raises `RuntimeError` in
    `step`; the malformed-plugin acceptance probe.
- Wired into `[project.optional-dependencies] dev` as a path dependency
  (`{root-uri}` form) so `uv sync --extra dev` — i.e. `make install` /
  every gates run — installs it. Unit tests do **not** need it (fake
  `EntryPoint`s, §1); integration tests assert the real
  installed-distribution → entry-points → allow-list → registry → API
  chain. Fallback if a path dep in an extra fights uv: a
  `[tool.uv.sources]` entry — same effect.
- Deliberate side effect: a developer's live backend already has the
  fixture installed; the walkthrough only sets the allow-list env var.

### 6. Capabilities + web (UI enablement)

- `_SECTION_FLAGS["plugins"]` flips `enabled: True`; the section detail
  is derived, registries-mirror-registries:
  `{strategies: [StrategyInfo…], allowlist: [...]}` — including
  `missing` names and `failed` imports. No new route: the Plugins
  section renders from capabilities (same pattern as models/tools).
- Web: new `web/src/sections/plugins/` — a read-only listing (name,
  origin badge, version, status) plus an allow-list panel that
  displays the live allow-list and the env var instructions for
  changing it. The allow-list is **server config, not API state** — the
  UI displays it, never edits it (backend gates are never relaxed for
  UI, rule 6).
- `make gen-api` regen widens the generated TS `strategy.type` union to
  `string`; the editor's cast (`AgentEditor.tsx:341`) keeps working and
  its options list picks plugins up automatically.
- Web-test gotcha carried from S10: `renderWithProviders` SEEDS
  `TEST_CAPABILITIES` — the seed needs the plugins section + a plugins
  detail; `capabilities: null` exercises the fetch path.

### 7. Rule-7 audit (why no ADR)

- `ports/` — untouched. `AgentStrategy`/`StepOutcome`/`StrategyRegistry`
  are exactly as ADR 0004 froze them; the runtime consumes the
  Protocol's documented raise (`KeyError`) instead of adding a typed
  exception to the port.
- Event envelope — untouched. No new event types; one new
  `error_kind` value, same field, ADR 0003 precedent.
- Error shape — untouched (the create-time 422 mirrors what the Literal
  already produced).
- Domain — `StrategyConfig.type` widens (an ADR-0002-compatible
  superset; version snapshots are the compatibility proof, tested).
- Everything new lives in internal layers (`strategies/`, `config.py`,
  `api/`, `web/`) plus two decisions and one contract doc. CLAUDE.md
  rule 7 is satisfied by the decisions log; if review disagrees, the
  fallback is a short ADR 0012 — the design itself doesn't change.

## Commit sequence (each independently green)

Gates before **every** commit: `pytest tests/unit`, `ruff check src
tests`, `mypy src`; web commits also `tsc --noEmit`, eslint, vitest.

1. **docs: stage record** — this plan doc, D35/D36 in
   `docs/decisions.md`, `docs/plugins/strategy-plugins.md` (contract).
   No code.
2. **feat(domain,api,cli): open the type** — `StrategyConfig.type`
   → `str`; API create/update validates against the live registry (422
   naming known strategies); `jarvis agent create` same check, friendly
   error. Tests: domain accepts arbitrary types; API rejects unknown
   with the known list; existing create payloads unchanged.
3. **feat(strategies,config): discovery + allow-list** —
   `strategy_plugin_allowlist` setting (CSV validator), the loader,
   `describe()` + plugin info on the registry, container wiring (load
   failures carried, never raised), the fixture distribution + dev-extra
   path dep. Unit tests via fake `EntryPoint`s: allow-list gate,
   not-allow-listed skipped, import-failure recorded, missing recorded.
4. **feat(runtime): strategy failure kind** — `error_kind="strategy"`
   at both catch sites (§3). Unit tests through the real loop with the
   mock provider: `raise_plugin` → exactly one terminal `run.failed`
   (kind `strategy`), worker acks (no retry); unknown type on a pinned
   snapshot → same.
5. **feat(api,capabilities) + integration** — plugins section flips
   enabled with derived detail; capabilities unit tests; integration:
   allow-listed `plan_execute` agent created and run through the API
   end-to-end (mock provider, scripted plan/execute/DONE turns);
   de-allow-list → create 422s, existing version runs to a persisted
   `strategy` failure.
6. **feat(web): Plugins section** — gen-api regen, the section page,
   TEST_CAPABILITIES seed, vitest (listing renders, allow-list panel,
   coming-soon path gone).
7. **docs: closure** (after Harshit live-verifies) —
   `docs/walkthrough-s3.md` (§10), README plugins section, roadmap S3
   marked done, stage-closed memory/CLAUDE.md notes as needed.

## Risks & deliberate deferrals

- **No hot load/unload** — a restart moves the installed/allow-listed
  set; stated in the contract doc, shown in the UI copy.
- **Stateless-only plugins** — shared instances; per-resolve factories
  deferred (the registry docstring's own escape hatch).
- **No plugin API versioning** — no semver gate on jarvis imports; the
  ADR rule is the only stability promise. Revisit if a real ecosystem
  appears.
- **Allow-list editing via UI** — out of scope by construction (env is
  the config plane; rule 6).
- **Anti-scope** (frontend-architecture table): tool/model plugins,
  marketplace, per-strategy params schema UI — S3 is strategies only.
- **uv path-dep-in-extra** is the one unverified tooling assumption;
  the `[tool.uv.sources]` fallback is equivalent work.
- The CLI `agent create` path now depends on the container's registry
  for validation — a headless CLI run against a DB with different
  allow-list env than the API server will disagree with it; that's
  inherent to env-based config and gets a line in the contract doc.

## Verification (S3 exit criteria — roadmap §S3 acceptance)

- The fixture `plan_execute` runs an agent through the API with **zero
  core changes** (integration test + live walkthrough).
- Malformed strategies cannot crash a run: `raise_plugin` and a
  missing/typo'd type each end in exactly one persisted terminal
  `run.failed` (`error_kind="strategy"`); the worker acks; no route
  ever 500s.
- `GET /v1/capabilities` reports the plugins section enabled with a
  truthful listing (builtins + plugins + missing + failed imports).
- The Plugins section is live in the web shell; the agent editor's
  strategy picker lists `plan_execute` without web-specific code.
- Gates green at every commit: unit suite, ruff, mypy; tsc, eslint,
  vitest at commits 2 (regen) and 6.

## Walkthrough draft (for `docs/walkthrough-s3.md`, live session)

0. `uv sync --extra dev` (fixture installed) → set
   `JARVIS_STRATEGY_PLUGIN_ALLOWLIST=plan_execute` in `./.env` →
   restart `jarvis serve` → `GET /v1/capabilities` shows plugins
   enabled, `plan_execute` listed, origin plugin.
1. Web Plugins section: listing + allow-list panel; agent editor
   strategy dropdown now offers `plan_execute`.
2. Create a `plan-execute-agent` (gemma4:31b, one harmless tool), run it
   from the console, watch plan → execute → DONE phases in the live
   transcript (think steps visible as assistant messages).
3. Allow-list `raise_plugin` too, restart; an agent pinned to it runs to
   a red `run.failed` with `error_kind="strategy"` in executions detail.
4. Remove everything from the allow-list, restart: capabilities no
   longer list plugins; creating an agent with `plan_execute` 422s with
   the known-strategy list; the existing pinned version still runs — to
   the same persisted `strategy` failure (snapshots outlive plugins).