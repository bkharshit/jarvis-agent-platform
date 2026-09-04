# Frontend Architecture — product shell with progressive enablement

> Status: planning doc, authoritative for the frontend. This supersedes the
> old model where the frontend waited for roadmap stage S5 (see
> `docs/roadmap.md` "Development model" and decision 1.6 in
> `docs/decisions.md`). **The full product shell ships now**, against the
> Phase 1 API, and each additional section lights up when the backend
> capability that powers it lands.

## Development model

```
full product structure early ──► backend/API capability ──► enable its UI ──► next capability
```

Rules:

1. **Design the whole IA up front; build only what the backend can honor.**
   Navigation, routing, layout, and section placeholders exist from day
   one. Screens for a section ship only when its API is real.
2. **The backend decides what is enabled.** The UI never hardcodes feature
   availability: it renders from `GET /v1/capabilities` (below). A section
   flips from coming-soon to live when — and only when — the backend says
   so. This is the frontend analog of the ports/ philosophy: the seam is
   the contract, not the promise.
3. **No fake functionality.** Disabled sections show a coming-soon panel
   naming what will live there and the roadmap stage that enables it. No
   mocked data, no inert buttons, no "works in the demo only". Every pixel
   renders real API state.
4. **Backend stages keep their own gates.** UI enablement is the *last*
   work item of each roadmap stage (S2, S4, S6, …), appended to its
   acceptance criteria — never a reason to rush the backend.

## Product IA (Dify-inspired)

Top-level navigation, one row per section. "Stage" = roadmap stage whose
backend work enables the section.

| Section | Dify analog | Backing API | Stage | At first ship |
|---|---|---|---|---|
| **Agents** — list, detail, editor, run console | Studio + app detail | `/v1/agents*`, `/run`, `/stream` | Phase 1 | ✅ enabled |
| **Executions** — runs list, detail, event replay | Logs (per app) | `/v1/executions*` | Phase 1 | ✅ enabled |
| **Conversations** — per-session transcripts | — | `/v1/conversations/…` | Phase 1 | ✅ enabled |
| **Tools** — builtin registry, bindings | Tools | tool descriptors via agent bindings | Phase 1 (builtins) | ✅ partial |
| **Models** — provider/model info | Settings → Model Providers | provider summary via `/v1/capabilities` | read-only info | ✅ read-only |
| **Workflows** — canvas builder, runs | Workflow canvas (ReactFlow) | workflow API | S6 | 🚧 coming-soon |
| **Knowledge** — datasets, retrieval | Knowledge | knowledge API | S8 | 🚧 coming-soon |
| **Evaluations** — datasets, runs, scores | — | eval API | S11 | 🚧 coming-soon |
| **Observability** — traces, spans | — | OTel backend | S7 | 🚧 coming-soon |
| **Plugins** — strategies (later: tools/models) | Plugins | plugin discovery API | S3 | 🚧 coming-soon |
| **Triggers** — cron/webhook/event rules | — | triggers API | S13 | 🚧 coming-soon |
| **Settings** — auth, tenants, API keys | Settings | auth API | S2 | 🚧 coming-soon (anonymous-mode notice) |

The shell runs in **anonymous/local mode** until S2; the Settings section
shows a "single-user local mode" notice rather than a fake login.

## Capability gating — `GET /v1/capabilities`

One new, deliberately tiny backend surface (rides with F1):

```json
{
  "sections": {
    "agents":       {"enabled": true,  "summary": "Create, version, and run agents"},
    "executions":   {"enabled": true},
    "conversations": {"enabled": true},
    "tools":        {"enabled": true,  "detail": {"mcp": {"enabled": false, "stage": "S4"}}},
    "models":       {"enabled": true,  "mode": "read-only"},
    "workflows":    {"enabled": false, "stage": "S6", "summary": "DAG runs reusing the same event model"},
    "settings":     {"enabled": false, "stage": "S2", "summary": "Auth, tenants, API keys"}
  }
}
```

- The nav and every section route render from this payload. Adding a
  backend capability = flipping its flag server-side; the frontend ships
  the section's screens in the same stage.
- Disabled sections render a shared `ComingSoon` component: section name,
  one-line summary, enabling stage. No per-section bespoke stubs.
- The payload is a fact of the backend (like the error envelope), so it
  can never lie about what exists.

## Tech stack

- **React + TypeScript + Vite** — `web/` directory in this repo.
- **TanStack Query** for server state (agents, executions, events);
  **zustand** for editor/canvas client state.
- **Typed API client generated from FastAPI's `/openapi.json`** — the
  backend models stay the single schema authority (same principle as ADR
  0002); no hand-duplicated request/response types.
- **One SSE client module** — framing, `Last-Event-ID` reconnect, terminal
  close, and delta coalescing in one place (frontend mirror of
  `api/sse.py`; Dify lesson: event handling centralized, not scattered).
- **Tailwind CSS** for styling.
- **@xyflow/react (React Flow)** — added **when S6 lands**, not before.
- **vitest + Testing Library**; Playwright e2e (run console + SSE resume)
  once F1 stabilizes.

Dify builder lessons adopted (from `docs/reference/dify-map.md`):
frontend registries mirror backend registries (tool/strategy lists come
from the API, never hardcoded); `_`-prefixed runtime state is stripped at
save; draft-save with a server hash for optimistic concurrency (agent
editor from F1; canvas at S6).

## App structure

```
web/
├── src/
│   ├── app/            # router, layout, capability-driven nav
│   ├── api/            # generated client, SSE client, error-envelope handling
│   ├── capabilities/   # section registry + ComingSoon component
│   ├── sections/       # one dir per IA section: agents/, executions/,
│   │                   #   conversations/, tools/, models/, workflows/, …
│   ├── components/      # shared UI (event timeline, JSON viewer, forms)
│   └── stores/         # zustand slices (editor drafts, run console state)
```

Routes at first ship:

| Route | Surface |
|---|---|
| `/agents` | list |
| `/agents/:id` | detail — tabs: Definition · Versions · Runs · API access |
| `/agents/:id/run` | run console (live SSE) |
| `/executions`, `/executions/:id` | runs list (filters), detail + event replay |
| `/conversations` | session browser + transcript |
| `/tools` | builtin descriptors (partial: MCP coming-soon) |
| `/models` | read-only provider/model info |
| `/workflows`, `/knowledge`, `/evaluations`, `/observability`, `/plugins`, `/triggers`, `/settings` | coming-soon panels |

## Key UX surfaces (F1)

- **Agent editor** — form + YAML view over `AgentDefinition` (model ref,
  strategy picker from the backend's registry, tool binding toggles with
  per-binding config like `allowed_hosts`, memory, limits). Save → the
  auto-published immutable version appears in the Versions tab.
- **Run console** — input box → `POST /stream`; live event timeline
  (coalesced `text.delta` text, tool-call cards with results, iteration
  markers); cancel button → `/executions/{id}/cancel`; disconnect/reconnect
  resumes via `Last-Event-ID`; finished runs replay identically from
  `/executions/{id}/events`.
- **Executions browser** — status/agent/session filters; detail shows the
  transcript, tool executions, and full event log — everything the DB can
  reconstruct (this is the platform's proof-of-work surface).

## F1 work items (commit-style; gates per commit: `tsc --noEmit`, eslint, vitest)

1. `feat(web): scaffold Vite + React + TS app, Tailwind, gates` 
2. `feat(api): add GET /v1/capabilities` (backend; unit + integration tests)
3. `feat(web): capability-driven shell + ComingSoon pattern`
4. `feat(web): generated API client + error-envelope handling`
5. `feat(web): agents list + editor`
6. `feat(web): versions tab`
7. `feat(web): run console with live SSE, cancel, Last-Event-ID resume`
8. `feat(web): executions list/detail + event replay`
9. `feat(web): conversations browser`
10. `feat(web): tools + models (partial/read-only)`
11. `test(web): e2e smoke against the mock provider`

**Acceptance:** an agent can be created, edited, versioned, run, watched
live (SSE), cancelled, and replayed entirely through the UI; every disabled
section names its enabling stage; no screen renders data the API didn't
return.

## Per-stage UI enablement contract

Each backend stage's *last* work item flips on its section:

| Backend stage | UI flips on |
|---|---|
| S1 | no new section — run console/executions must pass unchanged against the queue-backed sink |
| S2 | **Settings** (login, API keys, tenants); shell drops anonymous notice |
| S3 | **Plugins** (strategy listing + allow-list config) |
| S4 | **Tools → MCP** (servers, `mcp__*` bindings alongside builtins) |
| S6 | **Workflows** canvas (React Flow; the superseded S5 core) |
| S7 | **Observability** (trace list + span view keyed by `trace_id`) |
| S8 | **Knowledge** (datasets) + knowledge binding in the agent editor |
| S10 | Run console: `run.awaiting_input` approval/input cards + resume; awaiting-input inbox |
| S11 | **Evaluations** (datasets, scores, version comparison) |
| S12 | Memory strategy options in the agent editor |
| S13 | **Triggers** (cron/webhook/event rules) |
| S14 | Sub-agent / handoff bindings in the agent editor |

## What we deliberately do NOT build (anti-scope)

- Screens for unbuilt sections beyond the shared `ComingSoon` panel —
  speculative UI is where rot and rework live; the IA bet is kept cheap
  (sections, routes, and panels only).
- Auth UI before S2 — the shell runs anonymous/local mode.
- React Flow / canvas infrastructure before S6.
- localStorage "demo mode" data or seeded fixtures — the UI always shows
  real API state, even when that state is empty.