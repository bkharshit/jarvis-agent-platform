import { SectionGate } from "@/capabilities/SectionGate";
import { modelDefaults, modelProviders } from "@/capabilities/detail";
import { useCapabilities } from "@/capabilities/useCapabilities";

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
        {mode && (
          <span className="rounded-full bg-neutral-800 px-2 py-0.5 text-xs text-neutral-300">
            {mode}
          </span>
        )}
      </div>
      <p className="mt-1 text-sm text-neutral-400">
        Providers the runtime can resolve, with their declared capabilities.
      </p>

      {providers.length === 0 ? (
        <p className="mt-10 text-sm text-neutral-400">No providers registered.</p>
      ) : (
        <ul className="mt-6 flex flex-col gap-2">
          {providers.map((provider) => (
            <li key={provider.name} className="rounded border border-neutral-800 bg-neutral-900 p-4">
              <h2 className="font-mono text-sm text-neutral-100">{provider.name}</h2>
              <p className="mt-1 text-sm text-neutral-400">{provider.description}</p>
              {capabilityBadges(provider.capabilities).length > 0 && (
                <div className="mt-2 flex flex-wrap gap-1">
                  {capabilityBadges(provider.capabilities).map((badge) => (
                    <span
                      key={badge}
                      className="rounded bg-neutral-800 px-2 py-0.5 font-mono text-xs text-neutral-300"
                    >
                      {badge}
                    </span>
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
          <dl className="mt-2 rounded border border-neutral-800 bg-neutral-900 p-4 text-sm">
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