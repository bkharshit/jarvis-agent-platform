// One error type for every non-2xx response (decision 2). The backend
// envelope is frozen: {"error": {kind, message, details}} (D13/D16).
// 422 → per-field errors in forms via `fieldErrors`; 409 → toast with the
// server message verbatim; 404 → route-level not-found panel.

export interface ErrorEnvelopeBody {
  error?: {
    kind?: string;
    message?: string;
    details?: unknown;
  };
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly kind: string,
    message: string,
    readonly details?: unknown,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

/** Build an ApiError from a fetch Response (+ its parsed JSON body, if any). */
export function apiErrorFromResponse(response: Response, body: unknown): ApiError {
  const fallback = `request failed with status ${response.status}`;
  if (isEnvelope(body) && body.error) {
    return new ApiError(
      response.status,
      body.error.kind ?? "internal",
      body.error.message ?? fallback,
      body.error.details,
    );
  }
  return new ApiError(response.status, "internal", fallback);
}

function isEnvelope(body: unknown): body is ErrorEnvelopeBody {
  return typeof body === "object" && body !== null && "error" in body;
}

export interface FieldIssue {
  loc: (string | number)[];
  msg: string;
  type?: string;
}

/**
 * Map a 422 `details.errors` list to field-name → first-message, for inline
 * form errors. The leading `body` path segment (FastAPI's convention) is
 * stripped; the first segment after it names the top-level form field —
 * indices and segments below it are dropped, so a nested error like
 * `tools.0.name` files under `tools`, never under the agent's `name`.
 */
export function fieldErrors(error: unknown): Record<string, string> {
  const issues = extractIssues(error);
  const out: Record<string, string> = {};
  for (const issue of issues) {
    const field = fieldName(issue.loc);
    if (field && !(field in out)) out[field] = issue.msg;
  }
  return out;
}

function extractIssues(error: unknown): FieldIssue[] {
  if (!(error instanceof ApiError) || error.status !== 422) return [];
  const details = error.details as { errors?: unknown } | undefined;
  if (!Array.isArray(details?.errors)) return [];
  return details.errors.filter(
    (issue): issue is FieldIssue =>
      typeof issue === "object" && issue !== null && "loc" in issue && "msg" in issue,
  );
}

function fieldName(loc: (string | number)[]): string | null {
  const segments = loc.filter((part): part is string => typeof part === "string");
  if (segments[0] === "body") segments.shift();
  return segments.length > 0 ? segments[0] : null;
}