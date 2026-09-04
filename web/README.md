# JARVIS web — product shell (stage F1)

React + TypeScript + Vite frontend. Spec: `../docs/architecture/frontend-architecture.md`;
build plan: `../docs/implementation-plan-f1.md`.

## Running

```bash
make web-install          # npm install (once)
make web                 # Vite dev server (http://localhost:5173)
```

The dev server proxies `/v1` and `/healthz` to the backend at
`http://127.0.0.1:8000` — start it with `uv run jarvis serve` first. There is
no CORS middleware on the backend; the proxy is the single seam. (A
separately-deployed frontend later will need CORS added server-side.)

## Gates (every commit)

```bash
make web-test             # tsc --noEmit && eslint . && vitest run
make web-lint             # tsc --noEmit && eslint . only
```

## Generated API client (commit 4+)

`src/api/schema.d.ts` is generated from the backend's OpenAPI and committed:

```bash
make gen-api              # regenerate after any backend schema/route change
```

The backend is the single schema authority — never hand-duplicate request or
response types.

## E2e (commit 11+)

```bash
make web-e2e              # Playwright smoke; backend must be running already
```

Prerequisite: `uv run jarvis serve` against local Postgres (see
`../docs/local-run-guide.md`).