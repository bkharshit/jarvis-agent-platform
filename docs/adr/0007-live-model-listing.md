# ADR 0007 — Live Model Listing for the Agent Editor

- **Status**: Accepted
- **Date**: 2026-09-05
- **Extends**: ADR 0005 (single OpenAI-compatible adapter); D8-era
  capabilities contract (`GET /v1/capabilities` stays registry-derived)

## Context

The agent editor's **Model** and **Base URL** fields are free text. The
backend genuinely does not know which models an endpoint serves: the
`ModelProvider` port (`src/jarvis/ports/model.py`) declares
`generate`/`stream`/`capabilities` only, and the capabilities payload
carries provider names plus the environment default — not a model
inventory. The user (or YAML author) must know model ids out of band.

Every endpoint the OpenAI-compatible adapter targets — OpenAI, Ollama,
vLLM, LM Studio — exposes `GET {base_url}/models`. The information is
available at the adapter boundary; it is simply not surfaced.

Two constraints shape the decision:

- **The capabilities payload must stay registry-derived.** It is rendered
  on every page load and unit-tested with stub containers; adding a live
  network call there would slow and poison it (rule 6: it must never lie,
  and it must never hang). Live listing needs its own request path.
- **`ports/` is a frozen contract** (rule 7) — extending it requires this
  ADR.

## Decision

1. **`ModelProvider` gains `async def list_models(self) -> list[str]`.**
   Declared on the port, implemented per adapter:
   - `openai_compatible`: `GET {base_url}/models` with the same auth
     headers and timeout as chat calls; response `data[].id` values are
     returned (sorted). HTTP failures map onto the existing `ModelError`
     taxonomy (auth → `ModelAuthError`, 5xx/network →
     `ModelConnectionError`, …) via the adapter's existing error mapping.
   - `mock`: returns the ids of its scripted models (deterministic, no IO).

2. **`ModelProviderFactory` gains
   `async def list_models(provider, *, base_url=None, api_key_env=None)
   -> list[str]`.** The factory is already the single seam between "what
   an agent references" and "how a provider materializes" (ADR 0005); the
   route must not reach past it into adapter constructors. Unknown
   provider → `ModelError`.

3. **New route: `GET /v1/models`** with query parameters `provider`
   (required), `base_url` and `api_key_env` (optional; absent values fall
   back to the environment defaults, per D19 semantics). Response:
   `{"provider", "base_url", "models": [...]}`. Failure mapping onto the
   standard error envelope: unknown provider → 404 `not_found`;
   unreachable/failed listing → 502 `model_unreachable` with the
   provider's message truncated like other surfaced provider errors. A
   short timeout (10 s) bounds the request — this endpoint is
   interactive, not a run.

4. **Trust boundary is unchanged.** The runtime already fetches whatever
   `base_url` an agent definition names, with whatever `api_key_env`
   resolves to — a caller-supplied `base_url` to this endpoint adds no
   new SSRF surface for any principal trusted to define agents. The route
   is authenticated identically to the rest of the API (none until S2).

5. **The capabilities payload is untouched.** Provider names, the
   environment-defaults block, and the read-only `models` section flag
   stay exactly as they are. Live listing is a separate endpoint so the
   capabilities contract keeps its "no IO beyond registries" property.

6. **Frontend: the Model field becomes a combo box.** A `<datalist>` fed
   by `GET /v1/models` (react-query, keyed on provider + base_url +
   api_key_env) suggests real ids while free text stays valid — the
   listing can fail, the endpoint may not support it, and typed ids must
   never be blocked. A failed listing renders an inline message and
   leaves the field usable; it is never a fake-empty dropdown. New-agent
   drafts prefill Model/Base URL from the capabilities `defaults` block
   (real, env-derived data), and provider options show their capability
   payload descriptions.

## Consequences

- `ports/model.py` grows one method on `ModelProvider` and one on
  `ModelProviderFactory`; both are additive — no existing implementer
  breaks at the type level, and nothing `isinstance`-checks the
  runtime-checkable protocols.
- Model discovery is live data, not configuration: what the dropdown
  shows is what the endpoint answered, at the moment it answered. Stale
  or unreachable endpoints degrade to free text, never to silence.
- `GET /v1/models` is a read-only, stateless probe; it persists nothing
  and appears in no event stream.
- Native (non-OpenAI-shaped) providers implement `list_models` or raise
  `ModelError` — a provider that cannot enumerate says so through the
  same envelope rather than pretending.
- The editor's api_key_env field is honored for listings: authenticated
  endpoints (OpenAI cloud, ollama.com) return their real catalogs.