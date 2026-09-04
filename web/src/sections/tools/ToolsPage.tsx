import { SectionGate } from "@/capabilities/SectionGate";
import { builtinToolsFull, mcpGate } from "@/capabilities/detail";
import { useCapabilities } from "@/capabilities/useCapabilities";

// Tools registry — entirely payload-driven from the capabilities detail
// block. Nothing about the builtin set is hardcoded here; the MCP row
// reports the backend's own gate (enabled: false, stage: S4 today).

function ToolsInner() {
  const { data: capabilities } = useCapabilities();
  const tools = builtinToolsFull(capabilities);
  const mcp = mcpGate(capabilities);

  return (
    <div className="mx-auto max-w-4xl px-6 py-8">
      <h1 className="text-xl font-semibold">Tools</h1>
      <p className="mt-1 text-sm text-neutral-400">
        Builtin registry from the backend — agents bind these by name.
      </p>

      {tools.length === 0 ? (
        <p className="mt-10 text-sm text-neutral-400">No tools registered.</p>
      ) : (
        <ul className="mt-6 flex flex-col gap-2">
          {tools.map((tool) => (
            <li key={tool.name} className="rounded border border-neutral-800 bg-neutral-900 p-4">
              <h2 className="font-mono text-sm text-neutral-100">{tool.name}</h2>
              <p className="mt-1 text-sm text-neutral-400">{tool.description}</p>
              {Object.keys(tool.parameters).length > 0 && (
                <details className="mt-2">
                  <summary className="cursor-pointer text-xs text-neutral-500">
                    Parameter schema
                  </summary>
                  <pre className="mt-2 max-h-60 overflow-auto rounded bg-neutral-950 p-2 font-mono text-xs text-neutral-300">
                    {JSON.stringify(tool.parameters, null, 2)}
                  </pre>
                </details>
              )}
            </li>
          ))}
        </ul>
      )}

      {!mcp.enabled && (
        <p className="mt-8 rounded border border-neutral-800 p-4 text-sm text-neutral-400">
          MCP tools are not enabled yet
          {mcp.stage ? ` — they arrive in stage ${mcp.stage}` : ""}. This page
          reflects the backend's own gate.
        </p>
      )}
    </div>
  );
}

export function ToolsPage() {
  return (
    <SectionGate sectionKey="tools">
      <ToolsInner />
    </SectionGate>
  );
}