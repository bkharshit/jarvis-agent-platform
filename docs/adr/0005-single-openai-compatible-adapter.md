# ADR 0005 — One OpenAI-compatible adapter via base_url injection

- **Status**: Accepted
- **Date**: 2026-09-04

## Context

We need model access for dev/tests and for real providers, without N
provider adapters in Phase 1. Dify's model layer went through several
re-shapes (`api/core/model_manager.py` → plugin daemon → external `graphon`
package), partly because generate and streaming shared one triple-overloaded
`invoke_llm(stream=...)` union.

## Decision

1. **One** networked adapter: `OpenAICompatibleProvider` — plain `httpx`
   against `{base_url}/chat/completions`. `base_url` injection makes it serve
   OpenAI, Ollama (`http://localhost:11434/v1`), vLLM, LM Studio.
2. `generate()` and `stream()` are **separate methods** on `ModelProvider`
   — no stream-union overloads.
3. Plus a `MockModelProvider` (`provider: "mock"`) for tests and CLI demo —
   scripted turns, failure injection, records requests.
4. Typed error taxonomy (`ModelError` → Connection/Auth/RateLimit/
   BadRequest/Timeout/Aborted/Stream) with provider+model+status attached;
   retry only RateLimit + Connection (max 2, exponential backoff).
5. Capabilities are declared per provider (`ModelCapabilities`), e.g.
   Ollama's structured output is json-mode-only → schema-in-prompt fallback.

## Consequences

- Zero vendor SDKs; tests use respx against the adapter.
- A provider lacking function calling or streaming is representable, not
  discovered at runtime.
- Anthropic-native / Bedrock adapters can be added later behind the same
  `ModelProvider` protocol.