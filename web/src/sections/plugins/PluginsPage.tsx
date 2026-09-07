import { SectionGate } from "@/capabilities/SectionGate";
import { pluginListing, type StrategyInfo } from "@/capabilities/detail";
import { useCapabilities } from "@/capabilities/useCapabilities";

// Plugins — a read-only listing of the strategy registry (builtins + loaded
// plugins) plus the degenerate cases the backend reports (failed imports,
// missing names). The allow-list panel displays server config; the env var
// is the only way to change it — the API never edits it (rule 6).

function originBadge(origin: string): string {
  return origin === "builtin" ? "builtin" : "plugin";
}

function statusLine(info: StrategyInfo): string {
  if (info.error) return `import failed: ${info.error}`;
  if (info.distribution) {
    return `${info.distribution}${info.version ? ` ${info.version}` : ""}`;
  }
  return "core strategy";
}

function StrategiesInner() {
  const { data: capabilities } = useCapabilities();
  const listing = pluginListing(capabilities);

  return (
    <div className="mx-auto max-w-4xl px-6 py-8">
      <h1 className="text-xl font-semibold">Plugins</h1>
      <p className="mt-1 text-sm text-neutral-400">
        Loop strategies the runtime can resolve — core builtins plus
        third-party plugins loaded from installed packages via the
        <code className="mx-1 font-mono text-xs">jarvis.strategies</code>
        entry-point group.
      </p>

      {listing.strategies.length === 0 ? (
        <p className="mt-10 text-sm text-neutral-400">No strategies registered.</p>
      ) : (
        <ul className="mt-6 flex flex-col gap-2">
          {listing.strategies.map((info) => (
            <li
              key={info.name}
              className="rounded border border-neutral-800 bg-neutral-900 p-4"
            >
              <div className="flex items-center gap-2">
                <h2 className="font-mono text-sm text-neutral-100">{info.name}</h2>
                <span className="rounded bg-neutral-800 px-2 py-0.5 text-xs text-neutral-300">
                  {originBadge(info.origin)}
                </span>
                {info.error && (
                  <span className="rounded bg-red-900/60 px-2 py-0.5 text-xs text-red-200">
                    failed
                  </span>
                )}
              </div>
              <p className="mt-1 text-sm text-neutral-400">{statusLine(info)}</p>
            </li>
          ))}
        </ul>
      )}

      {listing.failed.length > 0 && (
        <section className="mt-8">
          <h2 className="text-sm font-medium text-neutral-300">Failed imports</h2>
          <ul className="mt-2 flex flex-col gap-2">
            {listing.failed.map((info) => (
              <li
                key={info.name}
                className="rounded border border-red-900/50 bg-neutral-900 p-4"
              >
                <h3 className="font-mono text-sm text-red-200">{info.name}</h3>
                <p className="mt-1 font-mono text-xs text-neutral-400">{info.error}</p>
              </li>
            ))}
          </ul>
        </section>
      )}

      {listing.missing.length > 0 && (
        <section className="mt-8">
          <h2 className="text-sm font-medium text-neutral-300">
            Allow-listed but not installed
          </h2>
          <p className="mt-2 font-mono text-sm text-amber-200">
            {listing.missing.join(", ")}
          </p>
        </section>
      )}

      <section className="mt-8">
        <h2 className="text-sm font-medium text-neutral-300">Allow-list</h2>
        <div className="mt-2 rounded border border-neutral-800 bg-neutral-900 p-4 text-sm">
          {listing.allowlist.length === 0 ? (
            <p className="text-neutral-400">
              Empty — no plugins load. Nothing loads unless it is named here.
            </p>
          ) : (
            <p className="font-mono text-xs text-neutral-200">
              {listing.allowlist.join(", ")}
            </p>
          )}
          <p className="mt-3 text-xs text-neutral-500">
            Server config, not API state. Add a plugin by installing its
            package, then setting
            <code className="mx-1 font-mono">JARVIS_STRATEGY_PLUGIN_ALLOWLIST</code>
            (comma-separated names) and restarting — there is no hot load.
          </p>
        </div>
      </section>
    </div>
  );
}

export function PluginsPage() {
  return (
    <SectionGate sectionKey="plugins">
      <StrategiesInner />
    </SectionGate>
  );
}