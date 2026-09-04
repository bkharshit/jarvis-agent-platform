// Thin JSON fetch over the Vite dev proxy. Commit 4 replaces the callers
// with the generated OpenAPI client; this module is the interim seam so no
// component ever calls `fetch` directly.

export class HttpError extends Error {
  constructor(
    readonly status: number,
    readonly kind: string,
    message: string,
    readonly details?: unknown,
  ) {
    super(message);
  }
}

export async function fetchJson<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(path, init);
  if (!resp.ok) {
    let kind = "internal";
    let message = `request failed with status ${resp.status}`;
    let details: unknown;
    try {
      const body = (await resp.json()) as { error?: { kind?: string; message?: string; details?: unknown } };
      if (body.error) {
        kind = body.error.kind ?? kind;
        message = body.error.message ?? message;
        details = body.error.details;
      }
    } catch {
      // non-JSON body — keep the generic message
    }
    throw new HttpError(resp.status, kind, message, details);
  }
  return (await resp.json()) as T;
}