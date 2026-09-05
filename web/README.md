# JARVIS web (stage F1)

React 19 + Vite + TypeScript + Tailwind v4 + TanStack Query. The product
shell renders from `GET /v1/capabilities` — unimplemented sections are
coming-soon, never faked.

## Running locally (verified 2026-09-05)

Port 8000 is frequently taken on this machine by another project, so the
verified setup runs the backend on **:8001** and points the dev proxy at it
via `JARVIS_API_URL`. The mock provider needs no network and no API key.

```bash
# 1. Backend (migrations run automatically at startup; needs local Postgres
#    — no Docker on this machine, role jarvis/jarvis, db `jarvis`)
JARVIS_PORT=8001 JARVIS_MODEL_PROVIDER=mock JARVIS_MODEL_NAME=mock-agent \
  uv run jarvis serve

# 2. Web dev server, proxy seam pointed at :8001 (default is 127.0.0.1:8000)
cd web && JARVIS_API_URL=http://127.0.0.1:8001 npm run dev

# 3. Open http://localhost:5173 — nav, agents, runs, executions, replay,
#    conversations, tools/models are live; disabled sections name their stage.
```

Why each env var:

- `JARVIS_PORT=8001` — :8000 is often occupied by an unrelated project on
  this machine; keep them side by side instead of killing that process.
- `JARVIS_MODEL_PROVIDER=mock` / `JARVIS_MODEL_NAME=mock-agent` — scripted
  provider, no network, deterministic replies. Agent YAML/forms pin their own
  provider anyway; these only fill provider-less defaults.
- `JARVIS_API_URL` — overrides the Vite proxy target (`vite.config.ts`) so
  the browser talks to :8001 through the same-origin seam. There is still no
  CORS middleware by design; the proxy is the single seam.

## Test gates

```bash
make test        # backend unit suite (no DB, no network, no LLM)
make web-test    # tsc --noEmit && eslint && vitest (80 tests)
make web-e2e     # Playwright smoke — needs the backend running (below)
```

## Regenerating the API client

The backend is the schema authority. After changing `src/jarvis/api/schemas.py`
or any route:

```bash
make gen-api   # dumps web/src/api/openapi.json → openapi-typescript → schema.d.ts
```

Both generated files are committed; the client (`src/api/client.ts`) is typed
against them via `openapi-fetch`. No hand-written response shapes in `src/api/`
(enforced by eslint — `any` is banned there).

## Dev proxy

`vite.config.ts` proxies `/v1` and `/healthz` to the backend (default
`127.0.0.1:8000`, override with `JARVIS_API_URL`). That proxy is the single
seam to the backend — no CORS middleware on the FastAPI app. A
separately-deployed frontend later will need CORS; noted, not built.

## E2e smoke (Playwright)

```bash
# prerequisite 1: backend (the command from "Running locally" works as-is)
# prerequisite 2: the proxy target must match — Playwright starts Vite itself:
cd web && JARVIS_API_URL=http://127.0.0.1:8001 npx playwright test

# watch it in a visible browser window
cd web && JARVIS_API_URL=http://127.0.0.1:8001 npx playwright test --headed

# filmstrip + network/SSE inspector for each step
cd web && JARVIS_API_URL=http://127.0.0.1:8001 npx playwright test --trace on
npx playwright show-trace test-results/*/trace.zip
```

`playwright.config.ts` starts Vite itself via `webServer` (it reuses an
already-running Vite on :5173, which must have the matching
`JARVIS_API_URL`). The smoke creates a randomized mock `function_calling`
agent (memory on — the runtime creates the conversation row only for
memory-enabled agents), runs it over live SSE, then walks executions detail →
replay → conversations transcript → a disabled section naming its stage.
Rows are left behind on purpose.