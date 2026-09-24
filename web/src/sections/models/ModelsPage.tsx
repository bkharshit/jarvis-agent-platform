import { SectionGate } from "@/capabilities/SectionGate";
import { modelDefaults, modelProviders } from "@/capabilities/detail";
import { useCapabilities } from "@/capabilities/useCapabilities";
import { Badge } from "@/components/ui/Badge";
import { Card } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";

// Models — read-only provider info straight from the capabilities payload.
// The page never decides editability itself; the section's `mode` field is
// the backend's word, shown verbatim.

function capabilityBadges(caps: Record<string, unknown>): string[] {
  return Object.entries(caps).map(([key, value]) =>
    typeof value === "boolean" ? `${key}: ${value ? "yes" : "no"}` : `${key}: ${String(value)}`,
  );
}

function ModelsInner() {
  const { data: capabilities } = useCapabilities();
  const providers = modelProviders(capabilities);
  const defaults = modelDefaults(capabilities);
  const mode = capabilities?.sections.models?.mode ?? null;

  return (
    <div className="mx-auto max-w-4xl px-6 py-8">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">Models</h1>
        {mode && <Badge>{mode}</Badge>}
      </div>
      <p className="mt-1 text-sm text-neutral-400">
        Providers the runtime can resolve, with their declared capabilities.
      </p>

      {providers.length === 0 ? (
        <EmptyState
          title="No providers registered."
          description="The backend reports an empty provider registry."
        />
      ) : (
        <ul className="mt-6 flex flex-col gap-2">
          {providers.map((provider) => (
            <li
              key={provider.name}
              className="rounded-xl border border-neutral-800/80 bg-neutral-900/50 p-4 shadow-sm transition-all hover:border-neutral-700/80"
            >
              <h2 className="font-mono text-sm text-neutral-100">{provider.name}</h2>
              <p className="mt-1 text-sm text-neutral-400">{provider.description}</p>
              {capabilityBadges(provider.capabilities).length > 0 && (
                <div className="mt-2 flex flex-wrap gap-1">
                  {capabilityBadges(provider.capabilities).map((badge) => (
                    <Badge key={badge} className="font-mono">
                      {badge}
                    </Badge>
                  ))}
                </div>
              )}
            </li>
          ))}
        </ul>
      )}

      {defaults && (
        <section className="mt-8">
          <h2 className="text-sm font-medium text-neutral-300">Environment defaults</h2>
          <Card className="mt-2 p-4 text-sm">
            <dl>
              <div className="flex gap-2">
                <dt className="w-24 shrink-0 text-neutral-400">Provider</dt>
                <dd className="font-mono text-xs text-neutral-200">{defaults.provider}</dd>
              </div>
              <div className="mt-1 flex gap-2">
                <dt className="w-24 shrink-0 text-neutral-400">Model</dt>
                <dd className="font-mono text-xs text-neutral-200">{defaults.model}</dd>
              </div>
              <div className="mt-1 flex gap-2">
                <dt className="w-24 shrink-0 text-neutral-400">Base URL</dt>
                <dd className="font-mono text-xs text-neutral-200">
                  {defaults.base_url ?? "provider default"}
                </dd>
              </div>
            </dl>
          </Card>
        </section>
      )}
    </div>
  );
}

export function ModelsPage() {
  return (
    <SectionGate sectionKey="models">
      <ModelsInner />
    </SectionGate>
  );
}