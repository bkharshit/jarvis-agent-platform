# ADR 0020 — Knowledge / RAG (S8): datasets, an embeddings port, and two retrieval surfaces

Status: accepted (planning ADR — no S8 code exists at time of writing)
Date: 2026-09-18
Supersedes: none (implements the roadmap S8 sketch; scope confirmed by
Harshit 2026-09-18 — pgvector vector store (Homebrew local install +
pgvector compose image), text-document ingestion only, local Ollama
embeddings via a dedicated embedding model (`qwen3-embedding:0.6b`
nominated — build commit verifies/pulls), BOTH the knowledge binding
and the `retriever__` tool family, and a retrieval abstraction that
admits hybrid retrieval / reranking / metadata filtering later WITHOUT
touching the agent runtime)

## Context

Agents today know only what fits in their system prompt and what tools
fetch live. The roadmap S8 sketch asks for retrieval-augmented agents:
tenant-owned knowledge datasets (documents → chunks → embeddings in
pgvector), retrieved context injected into the prompt through
`PromptEngine`, and a retriever tool family — "no runtime loop changes".

D47 (S12 planning) already deferred vector memory here and named the
seams: tenant-scoped store, per-segment resolution through the model
factory (the D28 pattern), recall injected through `PromptContext`
exactly like the summary.

Verified seams this design rides on (paths checked 2026-09-18):

- `prompt/engine.py:21-39` — `PromptContext` already carries
  `memory_summary: str | None` (the S12/D45 field); `build()` renders
  it as a second system message between the rendered system prompt and
  history (`engine.py:51-61`). A `knowledge_context` field parallel to
  it is the D47-predicted seam.
- `runtime/agent_runtime.py:295-297` — `_run_segment` loads memory
  (compaction BEFORE the new user message is appended) then builds the
  prompt; both inside the run's try (the D28 boundary). Retrieval
  slots between them.
- `runtime/agent_runtime.py:1013-1062` — `_rebuild_messages` (resume)
  does NOT call `build()`; it calls `render_system` directly. Anything
  injected only in `build()` would be MISSING on resumed segments —
  the injection must be mirrored here (and in resume's client resolve
  at `:815`/`:901`, both in-try).
- `ports/model.py:20-86` — `ModelProvider`/`ModelClient`/factory
  Protocols; NO embedding interface exists anywhere (verified by
  grep: zero `embed` outside `embedded_worker`). The OpenAI-compatible
  adapter speaks only `/chat/completions` and `/models`
  (`models/openai_compatible.py:130,191,255`).
- `models/factory.py:29-61` — `DefaultModelProviderFactory.resolve`
  is the D28 pattern: mock → MockModelProvider, stored credential →
  `_resolve_with_material` (principal REQUIRED), else
  `OpenAICompatibleProvider.for_ref`. `ModelRef` (`domain/agent.py:
  34-42`) already carries `credential_ref: CredentialRef | None` — an
  embeddings ModelRef reuses it verbatim.
- `tools/subagents/provider.py:253-360` — the ctx-capturing family
  provider pattern (`resolve(bindings, ctx)`) for tools that need
  run-scoped identity (principal) at execution time — ToolContext has
  no principal (`domain/tools.py:38-73`).
- `tools/mcp/errors.py:6-9` — the frozen promise: the three runtime
  catch sites catch `ToolResolutionError` and "one new provider adds
  its own subclass without touching them again."
- `runtime/agent_runtime.py:1230-1270` — `_resolve_tooling` merges
  exactly TWO families today (MCP + sub-agents, `_merged_close` takes
  two closers at `:1326-1339`). A third family extends this merge;
  this is the one structural seam (gated byte-identical when no
  `retriever__` bindings exist).
- `domain/agent.py:93-117` — `AgentDefinition` (`extra="forbid"`);
  versions snapshot the whole definition at publish
  (`persistence/repositories.py:126-150, 191-212` — D1), so a new
  optional binding field rides the snapshot with zero extra work.
- `ports/repository.py:266-317` — `TriggerRepo` is the strict-tenancy
  Protocol shape (required `tenant_id` kwargs, D29); `EvalRepo` rows
  show child rows may skip `tenant_id` when every read joins through
  the tenant-scoped parent (`repositories.py:1386-1388`).
- `api/routes/capabilities.py:50` — the `"knowledge"` section flag
  already exists disabled (`"stage": "S8"`); detail blocks derive from
  the container at request time. The S4/S13 lesson applies: the flag
  flip + derived detail ship in the SAME commit as the first table
  usage.
- Migrations `0001..0013` exist; next is `0014`. Tenant-table house
  style: `0011_eval_tables.py` / `0013_triggers.py` (explicit tenant
  FK + index, `server_default="default"`, typed JSONB columns,
  sa.Enum emitted by `op.create_table` only — D4).
- **Environment facts (checked 2026-09-18):** the local Postgres is
  Homebrew 16.15 with NO pgvector (`pg_available_extensions` empty);
  this machine has no Docker (compose.yaml's `postgres:17-alpine` is
  not what runs here). Integration tests self-migrate `jarvis_test`
  against the LOCAL Postgres (`tests/integration/conftest.py:47-71`)
  — so pgvector must be installed via `brew install pgvector` for
  anything to work, and the compose image swaps to
  `pgvector/pgvector:pg17` in the same commit so the migration is
  correct wherever Docker IS used. The local Ollama daemon
  (127.0.0.1:11434) serves OpenAI-compatible `/v1/embeddings`
  (verified live: the endpoint responds; `qwen3-embedding:0.6b` needs
  `ollama pull` first — no embedding model is pulled yet).

## Decision

### 1. Data model — three tables, immutable embedding config (D63)

Migration `0014_knowledge.py` (eval pattern, ADR 0002 typed JSONB):

- `knowledge_datasets` — `id` (uuid PK), `tenant_id` NOT NULL FK +
  index + `server_default="default"`, `name` UNIQUE per tenant,
  `description` text, `embedding_config` JSONB (typed
  `EmbeddingConfig`: `provider`, `model`, `base_url | None`,
  `credential_ref | None`, `dimension: int > 0`, `distance:
  Literal["cosine"]`), `chunk_config` JSONB (typed `ChunkConfig`:
  `chunk_chars = 1200`, `overlap_chars = 150`), `created_at`,
  `updated_at`.
- `knowledge_documents` — `id`, `dataset_id` FK ON DELETE CASCADE,
  `name`, `content` text (the re-chunk/re-embed source of truth),
  `status` (`knowledge_document_status` enum: `ready | error`),
  `error` text NULL, `chunk_count` int, `created_at`.
- `knowledge_chunks` — `id`, `document_id` FK ON DELETE CASCADE,
  `dataset_id` (denormalized for scoped search), `chunk_index` int,
  `content` text, `embedding` **pgvector `vector`** (dimension-free
  column type; the dimension is enforced at WRITE time from the
  dataset's pinned config, never by the column), `created_at`. HNSW
  index on `embedding` (`vector_cosine_ops`); `dataset_id` filters as
  a WHERE alongside (approximate-with-filter is fine at v1 scale —
  exact-scan fallback noted below).

The migration emits `CREATE EXTENSION IF NOT EXISTS vector` FIRST
(none exists today); downgrade drops the tables and the enum types
(D4 idiom: `DROP TYPE IF EXISTS`) but NEVER drops the extension (it
may be shared). `tests/integration/test_migrations.py` gains the
tables and must survive up/down/up.

**Dimension pinning is the load-bearing rule:** the embedding config
is IMMUTABLE after create (a PATCH that changes it is a 422 — the
eval wholesale-PATCH lesson means PATCH round-trips the full document
with `embedding_config`/`chunk_config` byte-identical or 422). Every
vector written to a dataset has the dataset's pinned dimension;
retrieval validates the query embedding against it. "Change the
embedding model" = create a new dataset (re-index tooling is
explicitly out of v1).

**Tenancy:** `KnowledgeRepo` Protocol with REQUIRED `tenant_id`
kwargs (the TriggerRepo shape). Documents and chunks carry no
`tenant_id` — every read joins through the tenant-scoped dataset
(the `eval_results` precedent, `repositories.py:1386-1388`), and
chunk search always WHEREs `dataset_id IN (…)` over tenant-visible
datasets only.

**Delete semantics:** a dataset DELETE is 409-refused while any
agent version snapshot references it (`snapshot #> '{knowledge,
datasets}' @> '["<id>"]'::jsonb` — the eval delete-with-runs
precedent); a document DELETE cascades its chunks (204). This keeps
the bound-dataset set stable for §4's one-embedding-model invariant.

### 2. The embeddings seam — a NEW port, never ModelClient (D64)

- Types: pure-pydantic `EmbeddingRequest` (`model`, `input:
  list[str]`) and `EmbeddingResponse` (`vectors: list[list[float]]`,
  `usage: Usage | None`) in `models/types.py` (`extra="forbid"`).
- `ports/model.py` gains one `@runtime_checkable` Protocol:
  `EmbeddingClient` with a single `async def embed(request) ->
  EmbeddingResponse` (pydantic/stdlib imports only — the dependency
  rule). **ModelClient is NOT extended**: it is what strategies
  receive and every implementer/test-double would break
  (`BoundModelClient`, `TracedModelClient`, providers, the
  SimpleNamespace fakes); embeddings are not part of the strategy
  loop's contract.
- Provider layer gains `embed()`: `OpenAICompatibleProvider.embed`
  → `POST {base_url}/embeddings` (reusing `_headers`/`_post`/
  `_raise_http_error`, `openai_compatible.py:90-97, 248-266`) — works
  for the local daemon and any OpenAI-compatible endpoint; the mock
  provider's embedding sibling (`MockEmbeddingProvider`) returns
  DETERMINISTIC hash-of-text → fixed-dim normalized vectors (stable
  similarity for hermetic tests), records requests, and can script
  errors (the MockModelProvider pattern, `models/mock.py:29-130`).
- `DefaultModelProviderFactory` gains `async def resolve_embedder(ref:
  ModelRef, *, principal) -> EmbeddingClient` — the D28/D47 pattern
  transposed: mock → MockEmbeddingProvider, stored credential →
  `_resolve_with_material` (principal REQUIRED, same as chat), else
  OpenAI-compatible. Per-call resolution, material never cached.
- The DATASET carries the embedding `ModelRef` inside its immutable
  config (provider/model/base_url/credential_ref/dimension persisted
  with the index — Harshit's scope answer). Nothing platform-level
  pins an embedding provider; `JARVIS_KNOWLEDGE_*` settings carry
  only caps/defaults (chunk sizes, max doc chars, top_k default,
  embed timeout, batch size — a `# --- knowledge (S8, ADR 0020) ---`
  settings section).
- JARVIS_LLM_TRACE does NOT wrap embeddings in v1 (the trace buffer
  wraps ModelClient.generate; recorded as deferred).

### 3. Chunking and ingestion — synchronous, text-only, honest (D65)

- **Ingestion surface: text documents only** (scope answer). A
  document is `{name, content}` JSON; the web editor reads files
  CLIENT-SIDE (FileReader → text) and posts the string — zero
  multipart machinery, zero parsing deps. File upload + PDF/docx
  parsing are recorded deferrals.
- **Chunker:** paragraph-aware packing (split on blank lines, pack
  into ≤ `chunk_chars`, `overlap_chars` tail carry; a single
  oversized paragraph hard-splits). One pure function over
  `(content, ChunkConfig)` — swappable seam for smarter chunking.
- **Ingestion is SYNCHRONOUS** (no queue, no worker role): POST
  documents → chunk → ONE batched `embed()` call (≤
  `JARVIS_KNOWLEDGE_EMBED_BATCH` texts per request) → persist
  document + chunks in one tx → `201 {document, chunk_count}` with
  `status: "ready"`. On embedding failure (provider unreachable,
  auth, dimension mismatch): the document row PERSISTS with
  `status: "error"` + the error text, and the route answers
  `502 embedding_unavailable` (the probe's `model_unreachable`
  precedent) — honest and retryable (delete + re-post in v1; a
  reindex endpoint is deferred). Content cap: `JARVIS_KNOWLEDGE_MAX_DOC_CHARS`
  (default 200_000) → 422 at the boundary.
- Eval children / trigger-fired / sub-agent child runs need nothing:
  they resolve the binding inside the same `_run_segment` (verified:
  ordinary queue runs through the unchanged worker).

### 4. The knowledge binding — auto-injected context (D66)

- `AgentDefinition` gains ONE optional field (additive,
  `extra="forbid"`-compatible, rides the D1 snapshot automatically):
  `knowledge: KnowledgeBinding | None` = `{datasets: list[str] (ids),
  top_k: int = 4, min_score: float | None}` — no `SearchConfig`
  strategy union in v1 beyond these knobs; §6 reserves the extension
  point.
- **One-embedding-model invariant (save-time 422):** all bound
  datasets must share the same embedding model + dimension, enforced
  at agent create/PATCH — a query embedding serves every bound
  dataset, so a mixed binding is unanswerable by construction. (The
  409 delete-refusal keeps the set stable afterwards.) A binding
  referencing a missing/foreign dataset is a 422 at save, and a lint
  on detail reads (the stale-pin lint family).
- **Injection:** `_run_segment` retrieves (embedding + SQL top-k)
  AFTER `_load_memory`, BEFORE `build()`, inside the try — then
  `PromptContext.knowledge_context` carries the rendered context and
  `build()` emits it as a system message between the memory summary
  and history (the `memory_summary` block, `engine.py:51-61`,
  byte-for-byte the parallel). `_rebuild_messages` mirrors it for
  resumed segments. `render_system` stays byte-identical (golden
  tests: fresh, resume, and no-knowledge byte-identical).
- **Failure semantics (D38, fifth application):** retrieval failure —
  embedder resolution, dataset gone, dimension drift — is a persisted
  terminal `run.failed` with the EXISTING `error_kind="tool"` naming
  the dataset/embedder (the missing-MCP-server precedent; no new
  error kind — `RunFailed.error_kind` lives in two places and stays
  frozen). No binding → the code path is skipped entirely
  (byte-identical gates).
- Workflow agent nodes get the binding for free (they ride
  `_run_segment`, `workflow_runtime.py:542-545`).

### 5. The `retriever__` tool family — model-directed retrieval (D67)

- Bindings named `retriever__<dataset>` (config: `{dataset_id,
  top_k}`), identity ALWAYS from `config.dataset_id`, never parsed
  from the name (the S14 rule). A provider in `tools/knowledge/`
  following `SubAgentToolProvider`: `resolve(bindings, ctx)` captures
  the full `ExecutionContext` (ToolContext has no principal), minting
  one tool per binding with a `{query: string}` parameter schema.
- Resolution failure (deleted/foreign dataset) →
  `RetrieverResolutionError(ToolResolutionError)` — the existing
  catch sites at `agent_runtime.py:242-246, 836-839` and
  `workflow_runtime.py:220-224, 298-299` need ZERO changes (the
  frozen promise in `tools/mcp/errors.py:6-9`). Call-time failures
  (embedder down mid-run) stay RECOVERABLE error ToolResults — the
  ToolRuntime envelope (jsonschema validation, timeout — default
  applies, no `-1` sentinel needed for a seconds-scale embed+search —
  50k truncation, `ToolResult.output` top-level) applies for free
  via `BaseTool`.
- `_resolve_tooling` extends to a third family (generalize the
  two-family merge + `_merged_close`); NO `retriever__` bindings →
  byte-identical tooling (golden gate). The family registers in the
  container beside MCP/sub-agents (`deps.py:138-154`).
- Workflow TOOL nodes stay MCP-only (as today for sub-agents too);
  agent nodes get the family via `_run_segment`. Recorded, not a
  gap: tool nodes bind MCP servers explicitly by design.

### 6. The retrieval abstraction — the runtime sees one call (D68)

**The contract (Harshit's scope answer):** the agent runtime and the
retriever tool know ONLY this shape —

```
retrieve(binding-or-config, query, tenant_id, principal) -> rendered context
```

implemented by one internal `KnowledgeRetrieval` service (embed via
`resolve_embedder` on the DATASET's config → SQL top-k with cosine
`ORDER BY embedding <=> :q LIMIT k` over tenant-visible datasets →
render `[<doc name>] content` blocks). Hybrid retrieval, reranking,
metadata filtering, and per-strategy scoring extend the SERVICE and a
future versioned `SearchConfig` union on the binding — WITHOUT
touching `agent_runtime`, the tool, the envelope, or `ports/`
(the strategy-config precedent: `StrategyConfig.type` validated at
the boundary, D36). v1 ships exactly one strategy: `vector`.

### 7. API, capabilities, web (D69/D70)

- `api/routes/knowledge.py` (mcp/eval precedent): datasets CRUD
  (201/200/204, D29 404s, 409 delete-while-referenced, 422 config
  immutability + same-model-set binding validation at agent save),
  documents create/list/delete, all behind the standard auth dep.
  Schemas beside the Trigger block in `api/schemas.py`; router
  registered in `app.py`.
- Capabilities: `_SECTION_FLAGS["knowledge"]` flips enabled WITH a
  derived `detail` (dataset + document counts) in the SAME commit as
  migration 0014's first usage — the S4 derived-facts 500 lesson.
  Unit stub containers updated in lockstep.
- Web: the `/knowledge` placeholder route swaps for a Knowledge
  section (datasets list → dataset detail: documents table, paste
  + read-file-as-text create, status chips, error display, delete
  with 409 toast — the Evaluations structure); the agent editor gains
  a knowledge-binding picker (multi-select datasets, top-k) gated on
  the capabilities accessor; `make gen-api` regen; msw handlers +
  capabilities fixtures flipped (`handlers.ts`, `TEST_CAPABILITIES`).

## Deliberately out of v1 (recorded, not silently dropped)

- File multipart upload and PDF/docx parsing (text-only in v1;
  client-side file reads dodge multipart entirely).
- Reindex / embedding-model migration on an existing dataset
  (config is immutable; new model = new dataset).
- Hybrid search, reranking, metadata filtering — the SEAM is §6's
  single-call contract; strategies ship when needed.
- Vector memory strategy (`MemoryConfig.strategy` stays
  `window | summarize`; D47's "vector when S8 lands" is reduced to
  this recorded deferral — knowledge datasets cover the recall need
  v1 targets).
- Per-chunk metadata (page/section provenance), cross-dataset
  dedup/rerank, async/queued ingestion for large corpora,
  embeddings in JARVIS_LLM_TRACE, dimension-typed vector columns +
  per-dimension HNSW indexes, workflow tool-node retrievers.

## What stays frozen

- `ports/tools.py` Protocols (`Tool`, `ToolRegistry`, `ToolRuntime`)
  — providers are adapters; neither MCP nor sub-agents touched
  `ports/`, and neither does the retriever.
- The event envelope (ADR 0003) and terminal semantics — existing
  `tool.*` event types only; no new event types, statuses, or
  `RunFailed.error_kind` literals ("tool" is reused).
- `ModelClient`/`ModelProvider` Protocols — embeddings ride a NEW
  `EmbeddingClient` port; `ports/model.py` gains it pre-declared
  here (CLAUDE.md rule 7).
- `RunQueueMessage`, the worker claim path, and the queue —
  ingestion is a route, retrieval is in-segment; nothing enqueues.
- `render_system` byte-identity (golden tests) — knowledge context
  rides a separate system message, exactly like `memory_summary`.
- The error envelope shape — route errors reuse existing kinds plus
  `embedding_unavailable` (route-level kind string, not the frozen
  `RunFailed.error_kind`).

## Consequences

- `brew install pgvector` becomes a documented environment
  prerequisite (walkthrough §0), and compose.yaml moves to
  `pgvector/pgvector:pg17` so the same migration works under Docker.
  The integration suite self-migrates, so the extension MUST exist
  before the first `-m db` run of migration 0014.
- `domain/knowledge.py` stays pgvector-free (vectors live in the
  persistence layer; the domain types end at `list[float]`
  boundaries) — a custom SQLAlchemy `UserDefinedType` (~30 lines,
  no new dependency) wraps the `vector` column rather than pulling
  `pgvector`'s python package.
- Agent snapshots now reference dataset ids loosely (no FK) — the
  409 delete-refusal plus detail lints are the drift story, the
  D57 provenance pattern one layer over.
- `AgentDefinition.knowledge` is additive and old snapshots stay
  valid (extra="forbid" + default None); the web editor sends it
  omit-empty like every other optional section.
- Tests stay hermetic: mock embeddings (deterministic vectors) +
  pgvector-backed integration for the SQL search path; the S11
  concurrent-children/mock-FIFO discipline applies to any
  knowledge-binding child runs.