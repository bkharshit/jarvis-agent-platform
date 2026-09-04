import type { components } from "@/api/schema";

// Re-derive the capabilities types from the generated schema — the backend
// is the schema authority, so this layer never hand-writes payload shapes.

export type SectionCapability = components["schemas"]["SectionCapability"];
export type Capabilities = components["schemas"]["CapabilitiesResponse"];