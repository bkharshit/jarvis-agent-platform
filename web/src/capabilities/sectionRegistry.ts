// One entry per IA section (docs/architecture/frontend-architecture.md).
// The registry fixes the nav order, titles, and routes; enablement is a
// backend fact read from GET /v1/capabilities — never hardcoded here.

export const SECTION_KEYS = [
  "agents",
  "executions",
  "conversations",
  "tools",
  "models",
  "workflows",
  "knowledge",
  "evaluations",
  "observability",
  "plugins",
  "triggers",
  "settings",
] as const;

export type SectionKey = (typeof SECTION_KEYS)[number];

export interface SectionMeta {
  title: string;
  route: string;
}

export const SECTION_REGISTRY: Record<SectionKey, SectionMeta> = {
  agents: { title: "Agents", route: "/agents" },
  executions: { title: "Executions", route: "/executions" },
  conversations: { title: "Conversations", route: "/conversations" },
  tools: { title: "Tools", route: "/tools" },
  models: { title: "Models", route: "/models" },
  workflows: { title: "Workflows", route: "/workflows" },
  knowledge: { title: "Knowledge", route: "/knowledge" },
  evaluations: { title: "Evaluations", route: "/evaluations" },
  observability: { title: "Observability", route: "/observability" },
  plugins: { title: "Plugins", route: "/plugins" },
  triggers: { title: "Triggers", route: "/triggers" },
  settings: { title: "Settings", route: "/settings" },
};

export function isSectionKey(key: string): key is SectionKey {
  return (SECTION_KEYS as readonly string[]).includes(key);
}