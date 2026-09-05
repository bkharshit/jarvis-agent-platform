# Stage F1 Walkthrough: Product Shell & Phase-1 UI

> Status: Authoritative walkthrough for Stage F1 (product shell), as required by the working agreement in `CLAUDE.md` before Stage S1 (distributed runs) begins.
> Landed across commits `cc13f33` through `4860dcc`.
> Backed by: `docs/implementation-plan-f1.md`, `docs/architecture/frontend-architecture.md`, `docs/decisions.md`, and `docs/adr/0001..0005`.

---

## 1. The Capability Contract & Product Shell

### What was built
- **Backend Route & Builder:** `src/jarvis/api/routes/capabilities.py` and schemas `CapabilitiesResponse` / `SectionCapability` in `src/jarvis/api/schemas.py`. It serves `GET /v1/capabilities` with a dictionary of 12 top-level product sections covering the entire information architecture: `agents`, `executions`, `conversations`, `tools`, `models`, `workflows`, `knowledge`, `evaluations`, `observability`, `plugins`, `triggers`, `settings`.
- **Client Route Guard & Gating UI:** `web/src/capabilities/SectionGate.tsx`, `ComingSoon.tsx`, `sectionRegistry.ts`, and query hook `useCapabilities.ts`.

### Why it is shaped that way
- **The Shell Renders from Backend Facts, Never Frontend Assumptions:** Gating is not hardcoded client-side. The backend returns a static dictionary of flags (`enabled`, `stage`, `summary`) and dynamic request-time `detail` blocks derived directly from the application container (`container.strategies.names()`, `container.tools.descriptors()`, `container.models.resolve()`).
- **Honest States with No Fake Functionality:** Per Philosophy 1.6, disabled sections render the shared `ComingSoon` panel naming the section and the specific roadmap stage that enables it (e.g. Workflows → `S6`, Settings → `S2`, MCP → `S4`). No fake mock data, inert buttons, or "demo-only" forms exist.
- **Honest Failure & Loading:** In `SectionGate.tsx`, while capabilities are loading or when the backend is unreachable (`isError`), the application displays full-screen honest statuses with a retry button. The navigation and routes never render as if everything were enabled or assume local fallbacks.

### Plan Decision & ADR Reference
- **Decision 1.6 & Plan Decisions 5 & 6:** Frontend ships alongside backend with progressive capability enablement; registries mirror backend registries; backend stays the single schema authority.

---

## 2. The API Seam & Error Envelope Handling

### What was built
- **Generated Client:** Snapshot schema `web/src/api/openapi.json` and generated TypeScript types `web/src/api/schema.d.ts` produced via `make gen-api` (`create_app().openapi()` -> `openapi-typescript`).
- **Fetch Wrapper:** `web/src/api/client.ts` wraps `openapi-fetch` and exports `client` and `unwrap()`.
- **Envelope-Aware Errors:** `web/src/api/errors.ts` defining `ApiError` and `fieldErrors()`.
- **Notification System:** Minimal hand-rolled Zustand store `web/src/stores/toast.ts` and `<Toaster />` component (`web/src/components/Toaster.tsx`).

### Why it is shaped that way
- **Backend Schema Authority:** Hand-written TypeScript interface duplicates of backend request/response payloads are strictly forbidden. The FastAPI route definitions and Pydantic models are the single source of truth (ADR 0002).
- **The `unwrap()` Abstraction:** Eliminates repetitive boilerplate and prevents raw `{ data, error }` leaks across UI components by throwing standard `ApiError` instances when `!response.ok`.
- **D13/D16 Error Envelope Parsing:**
  - **422 Unprocessable Entity:** Mapped via `fieldErrors(error)`. The FastAPI prefix `'body'` is stripped, and only top-level fields are kept (e.g. `['body', 'tools', 0, 'name']` maps to form field `'tools'`, preventing nested tool validation errors from improperly flagging the root agent name).
  - **409 Conflict:** Caught by mutations and surfaced directly to the user as verbatim toast errors (e.g., attempting to delete an agent with existing executions, or duplicate names).
  - **404 Not Found:** Renders dedicated inline route-level not-found / empty states.

### Plan Decision & ADR Reference
- **Plan Decisions 1 & 2, ADR 0002, Decisions D13 & D16.**

---

## 3. Agents CRUD & Editor: Draft-Store & Safe Patching

### What was built
- **Agent Editor & List:** `web/src/sections/agents/AgentEditor.tsx` and `AgentsList.tsx`.
- **Editor Draft Store:** `web/src/stores/editorStore.ts` handling full draft state, dirty flags, and conversions between domain entities and form drafts.
- **Dynamic Registry Pickers:** Strategy and tool pickers driven dynamically by `web/src/capabilities/detail.ts`.

### Why it is shaped that way
- **The PATCH `exclude_unset` Hazard (Plan Risk & Decision 8):**
  The backend PATCH endpoint uses `model_dump(exclude_unset=True)` combined with `model_copy()` without triggering re-validation of unset attributes. Sending explicit `null` for empty string or optional fields would poison snapshot integrity in the database.
  Therefore, `toSavePayload(draft)` in `editorStore.ts` strictly strips and **omits both `undefined` and empty/null keys** entirely:
  ```ts
  ...(draft.model.base_url.trim() !== "" ? { base_url: draft.model.base_url.trim() } : {})
  ```
- **Stripping Client State:** Because every API Pydantic schema enforces `extra="forbid"`, local editor transient state (`isDirty`, `draft.tools[].enabled`, etc.) is sanitized before generating the wire payload.
- **Dynamic Registry Pickers:** Available strategies (`function_calling`, `react`), tools (`calculator`, `current_time`, `http_get`), and providers (`mock`, `openai_compatible`) are read directly from `capabilities.sections.*.detail` via helper accessors in `detail.ts`. Nothing is hardcoded in the frontend.

### Plan Decision & ADR Reference
- **Plan Decision 8, Plan Decision 5, ADR 0002 (`extra="forbid"`), Decision D18.**

---

## 4. Versions Tab: Append-Only Immutable Snapshots

### What was built
- **Versions Tab Component:** `web/src/sections/agents/VersionsTab.tsx` embedded in `AgentDetailPage.tsx`.
- **Snapshot Viewer:** Side-by-side version list and formatted JSON snapshot inspection panel.

### Why it is shaped that way
- **No List Endpoint by Design:** The backend does not expose a `GET /v1/agents/{id}/versions` listing collection endpoint. Instead, the agent detail payload returned by `GET /v1/agents/{id}` contains `versions: VersionSummary[]`. The UI lists versions directly from `AgentDetail.versions`.
- **Snapshot Fetching:** When a user selects an individual version (`v1`, `v2`, etc.), `SnapshotPanel` queries `GET /v1/agents/{agent_id}/versions/{version}` to fetch the immutable definition snapshot.
- **Adherence to D1:** Versions are immutable snapshots saved at publish time. The UI displays them as historical, read-only proof of what the agent definition was at that specific point in time.

### Plan Decision & ADR Reference
- **Decision D1, Plan Decision 10.**

---

## 5. The Event Pipeline: Streaming, Resume & Projection

### What was built
- **SSE Client & Frame Parser:** `web/src/api/sse.ts` featuring `FrameParser` and `connectRunStream()`.
- **Pure Projection Reducer & Store:** `web/src/stores/runConsole.ts` exporting pure `applyEvent(state, event, cursor)` and `useRunConsoleStore`.
- **Run Console UI:** `web/src/sections/agents/RunConsole.tsx` and `web/src/components/EventTimeline.tsx`.

### Why it is shaped that way
- **`fetch` + `ReadableStream` instead of browser `EventSource`:**
  The browser's native `EventSource` only supports `GET` requests and cannot supply custom JSON request bodies or headers. JARVIS initiates runs with `POST /v1/agents/{agent_id}/stream` carrying `{ input, session_id, run_id }` and custom `Last-Event-ID`. Thus, `sse.ts` reads from the body `ReadableStream` directly using `TextDecoder`.
- **FrameParser Robustness:**
  Handles chunk fragmentation across packet boundaries (buffering until `\n\n`), parses `id:`, `event:`, and `data:`, joins multi-line `data:`, and silently ignores `: keep-alive` comment frames.
- **Durable Cursor Resume (ADR 0003 & Decision D15):**
  `Last-Event-ID` corresponds to the database's `execution_events.cursor` BIGSERIAL. If an HTTP connection drops before receiving a terminal event (`run.completed`, `run.failed`, `run.cancelled`):
  1. Stream end is **never** treated as run completion.
  2. The client automatically backs off and reconnects up to 3 times (300ms, 1000ms, 3000ms), posting the known `run_id` and passing `Last-Event-ID`.
  3. If retries exhaust, it transitions to `"disconnected"`, giving the user a manual **Resume** button. The server replays missed events from the database through the terminal event.
- **Pure Event Projection & Deduplication:**
  `applyEvent()` is a deterministic pure reducer. Reconnects might replay the tail of the stream; `applyEvent()` maintains `seenEventIds: Set<string>` to drop duplicates.
- **50ms Delta Coalescing:**
  LLM `text.delta` tokens arrive rapidly. Updating the React tree on every chunk can degrade browser frame rates. Deltas accumulate into `state.pendingText`, and a 50ms interval flushes text into consolidated message blocks. Calling `settle()` on abort/terminal flushes all remaining text immediately and clears the timer.
- **Parity Between Live SSE and Replay:**
  Live SSE stream frames and historical execution replay (`GET /v1/executions/{id}/events` JSON) feed into the **exact same `applyEvent` function**. There is zero divergence between real-time and post-hoc event visualizations.

### Plan Decision & ADR Reference
- **ADR 0003, ADR 0004, Decisions D3, D14, D15, Plan Decisions 3 & 4.**

---

## 6. Executions & Conversations

### What was built
- **Executions List & Detail:** `web/src/sections/executions/ExecutionsList.tsx` and `ExecutionDetail.tsx`.
- **Conversations List & Detail:** `web/src/sections/conversations/ConversationsList.tsx`, `ConversationDetail.tsx`, and grouping utility `groupSessions.ts`.

### Why it is shaped that way
- **Top-Level Tool Output (Decision D17):**
  In `ExecutionDetail.tsx`, tool execution cards render `t.output` directly from top-level properties instead of digging into nested `.result.output`.
- **Derived Conversations Index (Plan Decision 7):**
  The Phase 1 backend has a transcript endpoint (`GET /v1/conversations/{agent_id}/{session_id}`), but **no list-conversations endpoint**. Rather than creating speculative API endpoints in F1, `groupSessions.ts` aggregates sessions on the client from `GET /v1/executions` by `(agent_id, session_id)`. It caps at 50 sessions and displays an honest disclaimer if the total exceeds 50.
- **Memory-Enabled Agent Fact:**
  During the E2E run against the live backend, testing surfaced an important runtime rule: the runtime creates and updates `(agent, session)` rows in PostgreSQL **only when the agent has memory enabled (`memory.enabled: true`)**. Without memory turned on, running with a session ID executes fine, but does not persist a conversation session record for transcript browsing.

### Plan Decision & ADR Reference
- **Decision D17, Plan Decision 7, Commit `fa00707`.**

---

## 7. Tools & Models Pages: Payload-Swap Testability

### What was built
- **Tools Page:** `web/src/sections/tools/ToolsPage.tsx`.
- **Models Page:** `web/src/sections/models/ModelsPage.tsx`.

### Why it is shaped that way
- **100% Payload-Driven:**
  Neither the builtin tools list (`calculator`, `current_time`, `http_get`) nor the model provider configurations are hardcoded into JSX templates.
- **Payload-Swap Testability:**
  In `ToolsPage.test.tsx` and `ModelsPage.test.tsx`, tests inject arbitrary swapped capabilities payloads (e.g. replacing builtin tools with `web_search` or altering model providers). The components follow the data precisely.
- **MCP Gate from the Backend:**
  The Tools page includes an MCP tools notice stating that MCP tools arrive in stage `S4`. This message is not a static React string; it is gated dynamically by inspecting `capabilities.sections.tools.detail.mcp.enabled` and `detail.mcp.stage` from the server.

### Plan Decision & ADR Reference
- **Plan Decisions 5 & 10.**

---

## 8. Testing Strategy & Verification

### What was built
- **REST Mocking via MSW v2:** Configured in `web/src/test/msw.ts` and `setup.ts`.
- **Pure Function Tests:** `web/src/stores/runConsole.test.ts` (exercising all transitions, delta flushing, deduplication, and terminals).
- **SSE Stream Tests:** `web/src/api/sse.test.ts`.
- **Playwright E2E Smoke:** `web/e2e/smoke.spec.ts` and `playwright.config.ts`.

### Why it is shaped that way
- **MSW at the Fetch Layer:**
  Instead of mocking React Query hooks or custom client methods, MSW intercepts network calls. This directly exercises `openapi-fetch`, the generated client, error unwrap logic, and query cache lifecycles.
- **SSE Not Mockable via MSW:**
  Mock Service Worker struggles with long-lived streaming bodies in test environments. Instead, SSE testing is split cleanly into three deterministic layers:
  1. `FrameParser` chunk and boundary parsing tests.
  2. Pure `applyEvent` state projection unit tests.
  3. `connectRunStream` tested against a stubbed `globalThis.fetch` returning hand-built `ReadableStream` instances with controlled chunks.
- **What the Playwright Smoke Proves:**
  The single E2E smoke test runs against a real running FastAPI backend and local PostgreSQL (no Docker). It drives a complete product lifecycle:
  1. Creates an agent with `mock` provider and memory enabled.
  2. Verifies it in the agent list.
  3. Executes a run via live SSE stream and waits for `run.completed`.
  4. Inspects the execution in the executions browser.
  5. Triggers the event replay viewer.
  6. Opens the conversation transcript for the session.
  7. Confirms that disabled sections (`/workflows`, `/settings`) display their required roadmap stages (`S6`, `S2`).

### Plan Decision & ADR Reference
- **Plan Decisions 11 & 12.**

---

## Gotchas Worth Remembering

1. **PATCH Null Injection Hazard:**
   FastAPI/Pydantic `PATCH` models using `model_dump(exclude_unset=True)` do not re-run field validations on omitted keys. Sending an explicit `null` overwrites existing valid data with `None` in the immutable snapshot. Form saves must strip empty optional strings to `undefined` and omit them completely from the payload.
2. **MSW & jsdom `Request` Patching:**
   The browser client uses relative paths like `/v1/agents`. Node's `undici` `Request` implementation rejects relative URLs outside a browser. In `web/src/test/setup.ts`, `globalThis.Request` is monkey-patched to prepend `http://localhost`, while `client.ts` uses `fetch: (req) => globalThis.fetch(req)` to resolve `fetch` at call time (after MSW binds).
3. **Vitest Include Scope:**
   `web/vitest.config.ts` scopes test discovery strictly to `src/**/*.{test,spec}.{ts,tsx}`. This keeps `e2e/smoke.spec.ts` from being mistakenly executed during fast unit test runs (`make web-test`).
4. **Port 8000 Collision & `JARVIS_API_URL` Proxy Override:**
   Port 8000 is frequently claimed by other local applications. Commit `4860dcc` made the Vite proxy configurable via `JARVIS_API_URL` (defaulting to `http://127.0.0.1:8000`), allowing the backend to run on `JARVIS_PORT=8001` with `cd web && JARVIS_API_URL=http://127.0.0.1:8001 npm run dev`.
5. **Memory Requirement for Conversation Persistence:**
   The backend conversation store only writes session rows when `agent.memory.enabled` is `true`. A run with a session ID on a non-memory agent completes successfully, but will not show up in `/conversations`.

---

## Deliberate Deferrals & Residual Risks Left Behind by F1

- **No Backend CORS Middleware:** Communication between frontend and backend relies entirely on the Vite development proxy (`/v1` and `/healthz`). Production or separated deployments will require a formal CORS policy.
- **Client-Derived Conversations Listing:** Grouping executions client-side into conversation sessions is an explicit stopgap. A proper server-side `GET /v1/conversations` endpoint is deferred to a future backend stage.
- **Optimistic Concurrency (Draft Hashing):** The agent editor currently overwrites without a concurrency hash header (deferred to stage S6 workflow/canvas hardening).
- **Single E2E Smoke Spec:** E2E testing covers the critical happy path through the mock provider. Extensive edge-case browser testing (e.g. cross-browser, mobile layouts, network throttling) is deferred.

---

## Readiness Verdict for Stage S1 (Distributed Runs)

Stage F1 has successfully achieved all exit criteria: the entire 12-section product shell is live, the capability gating contract guarantees no faked functionality, the SSE and replay event pipelines are tested and unified under the pure `applyEvent` reducer, and full end-to-end flows are verified against local PostgreSQL. Because the frontend relies strictly on the frozen contracts of Phase 1 (`/v1/agents/{id}/stream`, `Last-Event-ID` cursor resumption, and the durable event envelope), **the system is fully ready to begin Stage S1**. Moving run execution out of the FastAPI process and onto a background queue worker during S1 will require zero structural changes to the frontend product shell.
