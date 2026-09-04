import createClient from "openapi-fetch";

import type { paths } from "./schema";
import { apiErrorFromResponse } from "./errors";

// Typed client over the Vite dev proxy (paths are same-origin). The
// generated types make the backend the schema authority — hand-written
// response shapes are forbidden in this layer.
export const client = createClient<paths>({
  // Defer the fetch lookup to call time: createClient captures
  // `globalThis.fetch` at construction (before msw patches it in tests).
  fetch: (request: Request) => globalThis.fetch(request),
});

type CallResult<TData> = {
  data?: TData;
  error?: unknown;
  response: Response;
};

/**
 * Unwrap one openapi-fetch call: return the parsed body on 2xx, throw the
 * envelope ApiError otherwise. Every hook goes through this so no component
 * ever sees the raw `{data, error}` shape.
 */
export async function unwrap<TData>(
  call: Promise<CallResult<TData>>,
): Promise<TData> {
  const { data, error, response } = await call;
  if (!response.ok) {
    throw apiErrorFromResponse(response, error);
  }
  return data as TData;
}