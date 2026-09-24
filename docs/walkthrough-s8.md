# S8 manual walkthrough — knowledge / RAG (ADR 0020), live script

A hands-on reproduction of the S8 acceptance behavior against a live
stack. Format follows walkthrough-s14. Backend :8001, web :5173, dev DB
`jarvis` (migration 0014). Mock agents where deterministic; real
`qwen3-embedding:0.6b` for the retrieval that must be semantically real,
real `gemma4:31b` for the runs whose receipts we read back through
llm-trace.

**Embeddings ride the LOCAL daemon, not Ollama Cloud.** Verified live
2026-09-24: `https://ollama.com/v1` serves `/v1/chat/completions` but
NOT `/v1/embeddings` — the embedding calls must point at
`http://localhost:11434/v1` (the local daemon, which has the model
pulled), with NO `credential_ref` (the local daemon takes no key).

S8 in one sentence: **agents can know things** — a dataset of embedded
text chunks is either auto-injected before each turn (D66) or called as a
`retriever__<dataset>` tool (D67), one shared retrieval service behind
both (D68), and every failure is an honest persisted row or terminal
state (D65: sync ingest never 201s a lie).

The invariants the walkthrough exercises:

- **D63 — the embedding config is pinned at create.** The dimension is
  load-bearing for every stored vector; a PATCH carrying any config key
  is a 422 at the schema boundary.
- **D65 — ingest is synchronous.** The POST blocks on chunk → embed →
  persist; an embedder failure persists an honest `error` row and
  answers 502 — the row is the receipt, never a silent 201.
- **D66 — the binding injects a system message.** The llm-trace receipt
  shows `Relevant knowledge base excerpts:` with rendered `[doc] content`
  blocks riding between the summary and the history.
- **D67 — the retriever family is a real tool.** The model calls it
  through the ordinary loop; identity is `config.dataset_id`, never the
  name.
- **D63 honesty — snapshots pin datasets.** A delete while any agent
  version snapshot references the dataset is a 409 with the reference
  count.

## 0. Setup

```bash
lsof -nP -iTCP:8001 -sTCP:LISTEN      # kill any stale backend first
brew list pgvector || brew install pgvector   # once; then restart Postgres
uv run alembic upgrade head           # migration 0014: knowledge tables + pgvector
ollama pull qwen3-embedding:0.6b      # if not already cached (LOCAL daemon)
uv run jarvis serve > /tmp/jarvis-8001.log 2>&1 &
sleep 3; curl -s localhost:8001/healthz
```

Embedder live-check BEFORE building anything on it (a dead embedder
turns every later step into a 502 — that's §4's script, not §1's).
The LOCAL daemon serves OpenAI-compatible /v1/embeddings and takes no
key; Ollama Cloud does NOT serve embeddings (see the header note):

```bash
curl -s http://localhost:11434/v1/embeddings \
  -H "Content-Type: application/json" \
  -d '{"model":"qwen3-embedding:0.6b","input":"hello"}' | head -c 200
```

auth_mode is `required` in `.env` — reuse your session identity, then:

```bash
export JARVIS_KEY="jarvis_sk_…"
export AUTH="Authorization: Bearer $JARVIS_KEY"
```

Add `JARVIS_LLM_TRACE=true` to `.env` (or export it before serve) — the
§2 receipts are the debug trace; without the flag the route returns
empty (ADR 0014).

## 1. Dataset create + text ingest

```bash
DS=$(curl -s -X POST localhost:8001/v1/knowledge/datasets -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{
    "name": "jarvis-notes",
    "description": "S8 walkthrough fixture",
    "embedding_config": {"provider":"openai_compatible","model":"qwen3-embedding:0.6b",
      "base_url":"http://localhost:11434/v1",
      "dimension":1024,"distance":"cosine"},
    "chunk_config": {"chunk_chars": 800, "overlap_chars": 100}
  }' | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
echo "$DS"
```

Config immutability, same commit:

```bash
curl -s -X PATCH localhost:8001/v1/knowledge/datasets/$DS -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"name":"renamed","description":"","embedding_config":{"provider":"mock","model":"x","dimension":8}}'
# → 422 (extra=forbid at the schema boundary)
```

Ingest three documents (paragraph-aware chunking, D65):

```bash
curl -s -X POST localhost:8001/v1/knowledge/datasets/$DS/documents -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"name":"platform.md","content":"JARVIS is an agent-runtime-first platform.\n\nAgents run versions, never drafts.\n\nRuns never raise."}' | python3 -m json.tool | grep -E 'status|chunk_count'
curl -s -X POST localhost:8001/v1/knowledge/datasets/$DS/documents -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"name":"models.md","content":"The default model is gemma4:31b via Ollama Cloud.\n\nEmbeddings are qwen3-embedding:0.6b, 1024 dimensions."}' | python3 -m json.tool | grep -E 'status|chunk_count'
```

Web: open the Knowledge section — dataset list → dataset detail →
documents table with `ready` chips and chunk counts. Paste a document in
the Add-document box and ingest from the UI. Optionally read a `.md`
file: its text lands in the content box (client-side FileReader), edit
before submit.

## 2. Knowledge-binding agent (auto-injection, D66)

Create + publish through the API (the same validation the web editor
rides):

```bash
AGENT=$(curl -s -X POST localhost:8001/v1/agents -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{
    "name": "kb-inject",
    "model": {"provider":"openai_compatible","model":"gemma4:31b",
      "base_url":"https://ollama.com/v1","credential_ref":{"type":"env","env_var":"OLLAMA_API_KEY"}},
    "strategy": {"type":"function_calling"},
    "knowledge": {"datasets":["'"$DS"'"], "top_k": 3}
  }' | python3 -c 'import sys,json;print(json.load(sys.stdin)["definition"]["id"])')
curl -s -X POST localhost:8001/v1/agents/$AGENT/run -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"input":"What model does JARVIS default to, and how many embedding dimensions?"}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["run_id"])'
```

The receipt:

```bash
curl -s localhost:8001/v1/executions/$RUN/llm-trace -H "$AUTH" | python3 -m json.tool | head -40
```

Iteration 0's `request.messages` carries a system message starting
`Relevant knowledge base excerpts:` with `[models.md] …` blocks — the
D66 seam, read from the ACTUAL request, not the built one. The final
message should answer both facts from the chunks.

Web: open the agent in the editor — the Knowledge fieldset shows the
dataset checked with its top-k.

## 3. Retriever tool run (model-directed, D67)

```bash
curl -s -X POST localhost:8001/v1/agents -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{
    "name": "kb-tool",
    "model": {"provider":"openai_compatible","model":"gemma4:31b",
      "base_url":"https://ollama.com/v1","credential_ref":{"type":"env","env_var":"OLLAMA_API_KEY"}},
    "strategy": {"type":"function_calling"},
    "tools": [{"name":"retriever__jarvis-notes","config":{"dataset_id":"'"$DS"'","top_k":3}}]
  }'
```

(If the binding is refused, the dataset id was mis-transcribed — the
save-time 422 names it.) Then run "Look up the platform's first rule in
the knowledge base and quote it." Expect an iteration whose tool call is
`retriever__jarvis-notes`, the tool result rendered `[models.md] …`
blocks (ToolResult.output is TOP-LEVEL in API JSON, D17), and the final
answer quoting the chunk.

## 4. Honesty paths

- **Mixed embedding setups → 422** (D66: one query embedding serves the
  binding). Create a second dataset with a different model/dimension,
  then PATCH an agent with both dataset ids → 422 "mixes embedding
  setups".
- **Delete-while-referenced → 409.** `DELETE /v1/knowledge/datasets/$DS`
  → 409, the message names the snapshot count. Delete the kb agents
  first, then the delete is 204.
- **Gone dataset → terminal tool failure.** Raw-SQL delete the dataset
  row (`DELETE FROM knowledge_datasets WHERE id='$DS'`) while the agent
  still pins it, then run → `run.failed` with `error_kind: "tool"` and
  "dataset not found in this tenant" (D29/D66).
- **Provider down → 502 with an error row.** Create a dataset with
  `base_url: http://127.0.0.1:9/v1`, ingest → 502
  `embedding_unavailable`; the documents listing then shows the `error`
  row with the failure text (the receipt, D65).
- **Tenancy.** A foreign dataset id reads as absent everywhere (404 on
  GET, 422 on bind, terminal tool on a stale snapshot).

## 5. Web tour

Knowledge section: list → create → detail → ingest (paste + file) →
status chips → error row display → delete (204 and the 409 toast). Agent
editor: knowledge multi-select + top-k; save omits the binding when
nothing is selected (the null-injection guard).

## 6. Teardown

```bash
# delete agents first (snapshots pin datasets), then datasets
curl -s -X DELETE localhost:8001/v1/knowledge/datasets/$DS -H "$AUTH"  # expect 409 until agents are gone
```

## 7. Covered by tests (not re-run live)

- Chunker + rendering + min_score/top_k selection (unit, deterministic
  hashed bag-of-words embedder).
- Repo: strict tenancy, one-tx documents, pgvector cosine ranking,
  dimension-mismatch DBAPIError, snapshot-reference delete guard.
- Routes: CRUD, config immutability 422, snapshot 409, ingest
  201/502-with-row/cap-422, tenant scoping (integration, -m db).
- Runtime: byte-identical prompts without a binding (golden), injection
  visible in built messages, resume retrieves against the row input,
  gone dataset terminal tool failure, sibling tooling close.
- E2E (-m db): llm-trace injection receipts, retriever loop, honesty
  paths, mixed-binding 422, no cross-tenant hits.

## 8. Session notes

(filled in during the session)
