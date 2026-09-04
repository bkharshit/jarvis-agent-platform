// The GET /v1/capabilities payload. Hand-typed until commit 4 lands the
// generated OpenAPI client; the backend is the schema authority (ADR 0002
// principle), so these mirror src/jarvis/api/schemas.py exactly.

export interface SectionCapability {
  enabled: boolean;
  mode?: string | null;
  summary?: string | null;
  stage?: string | null;
  detail?: Record<string, unknown> | null;
}

export interface Capabilities {
  sections: Record<string, SectionCapability>;
}