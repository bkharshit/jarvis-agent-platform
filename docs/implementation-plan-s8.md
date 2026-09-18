# Implementation plan — S8: knowledge / RAG (ADR 0020)

Format follows implementation-plan-s13.md. Every commit: gates green
(`uv run pytest tests/unit`, `uv run ruff check src tests`,
`uv run ruff format --check src tests`, `uv run mypy src`) before
committing; integration (`-m db`) where marked; web gates
(tsc --noEmit, eslint, vitest) on the web commit. One commit per step.
No S8 code exists at time of writing; all seams verified 2026-09-18.

## Verified seams (read in code, 2026-09-18)

See ADR 0020 "Context" for the full annotated list with line
references. The short version:

- **Prompt injection:** `PromptContext.memory_summary`
  (`prompt/engine.py:21-39`) renders as a second system message
  (`engine.py:51-61`); `knowledge_context` parallels it. `_run_segment`
  slots retrieval between `_load_memory` (`agent_runtime.py:295`) and
  `build` (`:297`); `_rebuild_messages` (`:1013-1062`) must mirror the
  injection for resumes.
- **Embeddings:** none exist. New `EmbeddingClient` port +
  provider-layer `embed()` (`POST {base_url}/embeddings`,
  `openai_compatible.py:90-97, 248-266` reusable) +
  `resolve_embedder` on the factory (`factory.py:29-61`, the D28
  pattern). Mock sibling: deterministic hash vectors
  (`models/mock.py:29-130` pattern).
- **Tool family:** providers are adapters, NOT ports
  (`ports/tools.py` untouched by MCP/sub-agents). Sub-agent provider is
  the ctx-capturing pattern (`tools/subagents/provider.py:253-360`);
  `RetrieverResolutionError(ToolResolutionError)` needs ZERO catch-site
  changes (`tools/mcp/errors.py:6-9` — catch sites
  `agent_runtime.py:242-246, 836-839`, `workflow_runtime.py:220-224,
  298-299`). `_resolve_tooling` (`agent_runtime.py:1230-1270`) merges
  exactly two families today — the one structural seam.
- **Data model:** migration next is `0014`; eval/trigger migrations are
  the tenant-table house style; child rows may skip tenant_id when every
  read joins the scoped parent (`repositories.py:1386-1388`); delete
  refusal with reference count (`repositories.py:1450-1468`).
- **Environment:** local Postgres = Homebrew 16.15, NO pgvector, no
  Docker on this machine (compose.yaml's postgres:17-alpine is not what
  runs here; conftest self-migrates jarvis_test against LOCAL Postgres,
  `tests/integration/conftest.py:47-71`). Local Ollama daemon serves
  OpenAI-compatible `/v1/embeddings` (live-verified endpoint; the
  nominated `qwen3-embedding:0.6b` must be `ollama pull`ed — no
  embedding model is present yet; only glm-5.3:cloud models are).
- **Capabilities:** `"knowledge"` flag exists disabled
  (`api/routes/capabilities.py:50`); derived detail + flip land in the
  SAME commit as first table usage (S4 lesson). Web `/knowledge`
  placeholder route exists (`routes.tsx:72`).
- **Golden gates exist:** `tests/unit/test_agent_runtime_golden.py`
  pins event sequences; `render_system` byte-identity is load-bearing
  for pre-S8 snapshots.

## Confirmed decisions (ADR 0020 carries the contract)

- **D63** — three tables (`knowledge_datasets` / `_documents` /
  `_chunks`), IMMUTABLE embedding config (dimension pinned at dataset
  create; config changes 422), strict-tenant KnowledgeRepo
  (required kwargs, TriggerRepo shape), child rows scoped via parent,
  409 delete-while-referenced (agent snapshots' `knowledge.datasets`
  JSONB containment check), document delete cascades chunks.
- **D64** — embeddings = NEW `EmbeddingClient` port (never a
  `ModelClient` extension) + provider-layer `embed()`; dataset carries
  its own embedding `ModelRef` (BYOK credential_ref reused);
  `resolve_embedder(ref, principal)` on the factory; MockEmbeddingProvider
  = deterministic hash vectors, scriptable errors; no LLM-trace wrap.
- **D65** — text-only ingestion (JSON content; web reads files
  client-side — no multipart, no parsing deps); paragraph-aware char
  chunker (pure function); SYNCHRONOUS ingest (chunk → batched embed →
  one-tx persist → 201 ready); provider failure persists the doc row
  `status="error"` + returns `502 embedding_unavailable`; content cap
  422.
- **D66** — `AgentDefinition.knowledge` optional binding
  (`{datasets, top_k, min_score?}`), rides the D1 snapshot; ONE
  embedding model across all bound datasets (save-time 422, detail
  lint for drift); injection via `PromptContext.knowledge_context` as
  a system message between summary and history, mirrored in
  `_rebuild_messages`; retrieval failure = persisted terminal
  `error_kind="tool"` (D38 fifth application) naming the resource; no
  binding → byte-identical.
- **D67** — `retriever__<dataset>` family (identity from
  `config.dataset_id`, never the name); ctx-capturing provider
  (SubAgentToolProvider pattern); resolution failure =
  `RetrieverResolutionError` (zero catch-site changes); call-time
  failures recoverable error ToolResults; `_resolve_tooling` merge
  generalized to three families, byte-identical gate when no
  `retriever__` bindings.
- **D68** — the retrieval contract: runtime and tool know only
  `retrieve(config, query, tenant, principal) -> rendered context`
  (one internal `KnowledgeRetrieval` service); hybrid/rerank/metadata
  strategies extend the service + a future versioned SearchConfig
  WITHOUT touching the runtime (Harshit's scope ask). v1 strategy:
  vector top-k, cosine `<=>`.
- **D69** — routes under `/v1/knowledge` (mcp/eval precedent, D29
  404s, wholesale full-document PATCH with immutable config fields);
  capabilities flip + derived detail in the SAME commit as migration
  usage; `# --- knowledge (S8, ADR 0020) ---` settings section.
- **D70** — web: Knowledge section (list → dataset detail with
  documents table, paste + read-file-as-text create, status chips,
  409 toast), agent-editor knowledge picker (multi-select, top-k,
  capability-gated), gen-api regen, msw flips.

## Commit sequence

1. **Environment + migration 0014.** `brew install pgvector` (run it,
   record in walkthrough §0 later); compose.yaml image →
   `pgvector/pgvector:pg17` (with a comment: local dev on this machine
   is Homebrew Postgres, no Docker); migration `0014_knowledge.py`:
   `CREATE EXTENSION IF NOT EXISTS vector`, three tables + HNSW index
   + `knowledge_document_status` enum (op.create_table emits the
   type — D4; downgrade DROP TYPE IF EXISTS, NEVER drops the
   extension); custom `VectorType(UserDefinedType)` in
   `persistence/`; `Settings` knowledge knobs
   (`JARVIS_KNOWLEDGE_CHUNK_CHARS`, `_OVERLAP_CHARS`, `_MAX_DOC_CHARS`,
   `_DEFAULT_TOP_K`, `_EMBED_BATCH`, `_EMBED_TIMEOUT`);
   `tests/integration/test_migrations.py` EXPECTED_TABLES + up/down/up
   green (`-m db`). **Blocks everything — the suite self-migrates.**
2. **Domain + ports.** `domain/knowledge.py`: `EmbeddingConfig`
   (provider/model/base_url/credential_ref/dimension/distance),
   `ChunkConfig`, `KnowledgeDataset`, `KnowledgeDocument`,
   `KnowledgeChunk`, `KnowledgeBinding`; `AgentDefinition.knowledge`
   (additive optional, snapshot-compatible — extend the
   `test_domain_models.py` "vector — S8, not yet" pin);
   `ports/repository.py`: `KnowledgeRepo` Protocol (required
   `tenant_id` kwargs; create/get/list/update/delete + document CRUD +
   `search_chunks(dataset_ids, embedding, limit)` +
   `count_dataset_references(dataset_id)`); `ports/model.py`:
   `EmbeddingClient` Protocol + `EmbeddingRequest`/`EmbeddingResponse`
   in `models/types.py`. Unit tests: domain validation, dimension
   rules, binding validation.
3. **Persistence.** ORM rows in `persistence/models.py` (typed
   JSONB contract); `SqlKnowledgeRepo` in `repositories.py`: tenant
   WHERE everywhere, pg_insert upserts where needed, document cascade
   delete, `search_chunks` = `ORDER BY embedding <=> :q` with
   dimension guard (write-side validation from dataset config),
   reference-count via `snapshot #> '{knowledge,datasets}' @>`
   containment; container field + wiring in `deps.py`. Integration:
   repo roundtrip + tenancy 404s + search ranking + dimension refusal
   (`-m db`).
4. **Embeddings adapters.** `OpenAICompatibleProvider.embed` (POST
   `{base_url}/embeddings`, batched input, error mapping to
   `ModelAuthError`/`ModelError` idioms); `MockEmbeddingProvider`
   (deterministic normalized hash vectors, request recording,
   scriptable error); `DefaultModelProviderFactory.resolve_embedder`
   (mock / stored-with-principal / openai_compatible — mirror
   `resolve`, `factory.py:42-61`); respx-based unit tests (hermetic,
   `Settings(_env_file=None)` discipline).
5. **Chunker + retrieval service.** `runtime/knowledge/chunking.py`
   (pure paragraph-aware packer, unit tests: packing, overlap,
   oversized-paragraph hard split, empty content); 
   `runtime/knowledge/retrieval.py` — the D68 service: embed query
   (batch of one) → repo.search_chunks → render `[doc name] content`
   blocks, min_score filter; mock-embedder unit tests (similarity
   ordering verifiable with deterministic vectors).
6. **Runtime integration (the risk commit — golden gates).**
   `PromptContext.knowledge_context` + engine system message
   (byte-identical `render_system`); `_rebuild_messages` mirror;
   `_run_segment` retrieval between `_load_memory` and `build()`
   (in-try; failure → `_terminal_failed(..., "tool", ...)`);
   `_resolve_tooling` three-family merge (byte-identical when no
   `retriever__` bindings — extend the golden tests);
   `tools/knowledge/provider.py` (`RetrieverToolProvider`,
   ctx-capturing, `RetrieverResolutionError`, minted `{query}`
   descriptor); container registration; agent save/PATCH route:
   binding validation 422 (same-model set, visible datasets) + detail
   lints; dataset delete route 409. Golden: fresh/resume/no-knowledge
   byte-identical; new: injection visible in built messages, terminal
   failure copy names the dataset.
7. **API routes + capabilities.** `api/routes/knowledge.py`: datasets
   CRUD (PATCH wholesale, config immutability 422, 409
   delete-while-referenced), documents create (synchronous ingest,
   201/502 embedding_unavailable, 422 content cap)/list/delete;
   schemas in `api/schemas.py`; router in `app.py`; capabilities
   `knowledge` flip + `_knowledge_detail` (dataset/document counts)
   SAME COMMIT (update `tests/unit/test_capabilities.py` stub);
   integration tests: CRUD, tenancy, ingest-happy (mock embedder in
   container), ingest-error 502 with persisted row, cap 422.
8. **End-to-end + honesty paths** (`-m db`). Mock-embedder e2e: agent
   with knowledge binding + mock model → run → final message reflects
   injected context (llm-trace receipts); retriever__ tool call in a
   function_calling run; deleted-dataset honesty (terminal tool
   failure copy); mixed-binding 422; dataset delete 409 while bound;
   dimension-mismatch refusal; provider-down 502; tenant isolation
   (foreign dataset 404 + no cross-tenant hits).
9. **Web.** `make gen-api` regen; `api/queries/knowledge.ts`; Knowledge
   section (`sections/knowledge/`): datasets list (create → detail,
   delete + 409 toast), dataset detail (documents table, paste +
   read-file-as-text create with client-side FileReader, status
   chips, error display, delete); agent-editor knowledge picker
   (multi-select from capabilities-gated dataset list, top-k input,
   omit-empty draft discipline); `capabilities/detail.ts` accessor;
   msw handlers + fixtures + capabilities flips (`handlers.ts`,
   `TEST_CAPABILITIES`); vitest. Gates: tsc, eslint, vitest.
10. **Manual session + docs closure** (post-build, with Harshit):
    `docs/walkthrough-s8.md` drafted during the build (uncommitted by
    design): §0 setup (`brew install pgvector` receipt, migration
    0014, `ollama pull qwen3-embedding:0.6b`, embedder live-check
    curl), §1 dataset create + text ingest (curl + web), §2
    knowledge-binding agent (real qwen3 embeddings + gemma4:31b —
    receipts via llm-trace: injected system message), §3 retriever__
    tool run (model-directed), §4 honesty paths (mixed binding 422,
    delete 409, terminal failure on gone dataset, 502 provider down),
    §5 web tour, §6 teardown, §7 unit-covered list, §8 session notes.
    Closure commit: walkthrough + session notes, README knowledge
    section + API rows, roadmap S8 shipped note, CLAUDE.md gotchas
    from the build.

## Rule-7 audit (what touches what)

| Touch | File(s) | Frozen-surface? |
| --- | --- | --- |
| NEW `EmbeddingClient` Protocol + embedding types | `ports/model.py`, `models/types.py` | **THE ports/ change #1 — pre-declared ADR 0020 §2** |
| NEW `KnowledgeRepo` Protocol | `ports/repository.py` | **THE ports/ change #2 — pre-declared ADR 0020 §1** |
| `AgentDefinition.knowledge` optional field | `domain/agent.py` | additive optional field, snapshot-compatible — pre-declared ADR 0020 §4 |
| knowledge domain types | `domain/knowledge.py` (new) | new module (domain, pgvector-free) |
| migration 0014 + VectorType UDT | `persistence/migrations/versions/`, `persistence/` | new tables; CREATE EXTENSION never dropped |
| `_resolve_tooling` third-family merge | `runtime/agent_runtime.py` | internal; byte-identical gate when no retriever bindings |
| `PromptContext.knowledge_context` + engine message + `_rebuild_messages` mirror | `prompt/engine.py`, `runtime/agent_runtime.py` | `render_system` byte-identical; golden tests gate |
| retriever family provider + error subclass | `tools/knowledge/` (new), `tools/mcp/errors.py` (add class only) | ports/tools.py untouched; catch sites untouched (frozen promise kept) |
| routes + capabilities + schemas | `api/routes/knowledge.py` (new), `capabilities.py`, `api/schemas.py`, `app.py` | additive routes; capabilities same-commit rule |
| Settings knobs | `config.py` | new section; CSV NoDecode pattern if any list knob |
| web section + editor picker | `web/src/sections/knowledge/` (new), `AgentEditor.tsx`, `routes.tsx` swap | capability-gated; gen-api regen |
| event envelope / event types / `RunFailed.error_kind` / `ModelClient` / `RunQueueMessage` / worker | — | **frozen** |

## Acceptance (roadmap S8, adapted to this scope)

- A tenant creates a dataset (mock embedder for tests, local
  `qwen3-embedding:0.6b` for the live session), ingests text
  documents, and chunks land in pgvector with pinned dimensions.
- An agent with a knowledge binding runs and its prompt provably
  contains retrieved context (llm-trace receipt); a `retriever__`
  binding gives the model an on-demand tool that returns chunks.
- Deleting a bound dataset is refused 409; deleting it after unbinding
  leaves runs' history intact; a gone dataset at run time is a
  persisted terminal `tool` failure, never an escape.
- A mixed-embedding-model binding is a 422 at save; a foreign dataset
  is a 404; retrieval never crosses tenants.
- No knowledge anywhere → byte-identical behavior (golden gates).
- `GET /v1/capabilities` flips `knowledge` with derived counts in the
  same commit as the migration's first usage; the web Knowledge
  section and agent-editor picker activate off it.
- Integration suite green with pgvector installed locally; migrations
  survive up/down/up.