# Model Layer

One OpenAI-compatible adapter via `base_url` injection, plus a mock
provider (ADR 0005).

## Types

- `ModelRequest` — `{model, messages, tools?, temperature, max_tokens?,
  response_format?, stop?}`. Tools are passed as JSON-Schema descriptors.
- `ModelResponse` — `{message (assistant; may carry tool_calls), usage,
  finish_reason, model}`.
- `StreamDelta` union — `text_delta | tool_call_delta{index, id?, name?,
  arguments_fragment} | usage_delta | finish_delta`.
- `ModelCapabilities` — `{streaming, function_calling,
  structured_output: none|json_mode|json_schema, parallel_tool_calls}`.

## Error taxonomy

`ModelError` base → `ConnectionError`, `AuthenticationError`,
`RateLimitError(retry_after)`, `BadRequestError`, `TimeoutError`,
`AbortedError`, `StreamError` — all carrying `provider`, `model`,
`status_code?`.

Retry policy (runtime-owned): retry **only** RateLimit + Connection, max 2
attempts, exponential backoff. Each attempt emits `model.invocation.started`
with `attempt`.

## Cancellation

The HTTP call runs wrapped in an `asyncio.Task`. The run's `CancellationToken`
event cancels the task; `CancelledError` maps to `ModelAbortedError` → the
orchestrator emits `run.cancelled`. The same token is shared with
`ToolContext`, so tools cancel in-flight too.

## Structured output

| capability | mechanism |
|---|---|
| `json_schema` | native `response_format: {type: json_schema}` |
| `json_mode` (e.g. Ollama) | `json_object` + schema rendered into the system prompt by `PromptEngine` |
| `none` | prompt-only rendering |

Post-parse validation against the agent's `output_schema`, then **one repair
retry** with the validation error appended as a developer message; on second
failure → `run.failed(error_kind="output_schema")`.