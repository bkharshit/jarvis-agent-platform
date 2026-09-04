# JARVIS web (stage F1)

React 19 + Vite + TypeScript + Tailwind v4 + TanStack Query. The product
shell renders from `GET /v1/capabilities` — unimplemented sections are
coming-soon, never faked.

## Commands

```bash
make web-install    # npm install
make web            # vite dev server (proxy /v1 + /healthz → 127.0.0.1:8000)
make web-test       # tsc --noEmit && eslint && vitest
make web-lint       # tsc --noEmit && eslint
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

`vite.config.ts` proxies `/v1` and `/healthz` to `127.0.0.1:8000`. That proxy
is the single seam to the backend — no CORS middleware on the FastAPI app.
Start the backend with `uv run jarvis serve`.
## E2e smoke (Playwright)

```bash
# prerequisite: a running backend with local Postgres (no Docker here)
uv run jarvis serve            # default :8000; if that port is taken:
JARVIS_PORT=8001 JARVIS_MODEL_PROVIDER=mock JARVIS_MODEL_NAME=mock-agent uv run jarvis serve

# point the Vite proxy at it and run
cd web && JARVIS_API_URL=http://127.0.0.1:8001 npx playwright test
```

`playwright.config.ts` starts Vite itself via `webServer`. The smoke creates a
randomized mock `function_calling` agent (memory on — the runtime creates the
conversation row only for memory-enabled agents), runs it over live SSE, then
walks executions detail → replay → conversations transcript → a disabled
section naming its stage. Rows are left behind on purpose.
