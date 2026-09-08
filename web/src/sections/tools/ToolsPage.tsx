import { useState } from "react";
import type { FormEvent } from "react";

import { useCreateMcpServer, useDeleteMcpServer, useMcpServers, useProbeMcpServer, useUpdateMcpServer } from "@/api/queries/mcp";
import type { McpServer } from "@/api/queries/mcp";
import { builtinToolsFull, mcpGate } from "@/capabilities/detail";
import { SectionGate } from "@/capabilities/SectionGate";
import { useCapabilities } from "@/capabilities/useCapabilities";
import { useWhoami } from "@/api/queries/auth";
import { toast } from "@/stores/toast";

// Tools registry — entirely payload-driven from the capabilities detail
// block. Nothing about the builtin set is hardcoded here. The MCP panel
// (S4) lists the tenant's servers live from the listing route; the
// capability gate only decides panel-vs-coming-soon.

const probeCard =
  "rounded border border-neutral-800 bg-neutral-900 p-3";

function McpTools({ serverId }: { serverId: string }) {
  const probe = useProbeMcpServer(serverId);
  if (probe.isPending) {
    return <p className="mt-2 text-xs text-neutral-400">Probing server…</p>;
  }
  if (probe.isError) {
    return (
      <p className="mt-2 text-xs text-red-400" role="alert">
        {probe.error.message}
      </p>
    );
  }
  const tools = probe.data.tools;
  return (
    <ul className="mt-2 flex flex-col gap-2">
      {tools.map((tool) => (
        <li key={tool.name} className={probeCard}>
          <h4 className="font-mono text-xs text-neutral-100">{tool.name}</h4>
          <p className="mt-1 text-xs text-neutral-400">{tool.description}</p>
          {Object.keys(tool.parameters ?? {}).length > 0 && (
            <details className="mt-1">
              <summary className="cursor-pointer text-xs text-neutral-500">
                Parameter schema
              </summary>
              <pre className="mt-1 max-h-60 overflow-auto rounded bg-neutral-950 p-2 font-mono text-xs text-neutral-300">
                {JSON.stringify(tool.parameters, null, 2)}
              </pre>
            </details>
          )}
        </li>
      ))}
    </ul>
  );
}

function McpServerRow({ server, canManage }: { server: McpServer; canManage: boolean }) {
  const update = useUpdateMcpServer();
  const remove = useDeleteMcpServer();
  const [showTools, setShowTools] = useState(false);

  const onError = (err: Error) => toast("error", err.message);

  return (
    <li className="rounded border border-neutral-800 bg-neutral-900 p-4">
      <div className="flex items-center justify-between gap-3">
        <div className="min-w-0">
          <h3 className="font-mono text-sm text-neutral-100">{server.name}</h3>
          <p className="mt-1 flex items-center gap-2 text-xs text-neutral-400">
            <span className="rounded border border-neutral-700 px-1.5 py-0.5 font-mono text-neutral-300">
              {server.config.type}
            </span>
            <span className={server.enabled ? "text-green-400" : "text-neutral-500"}>
              {server.enabled ? "enabled" : "disabled"}
            </span>
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-3 text-xs">
          <button
            type="button"
            onClick={() => setShowTools((v) => !v)}
            className="cursor-pointer text-neutral-400 hover:text-neutral-200"
          >
            {showTools ? "Hide tools" : "View tools"}
          </button>
          {canManage && (
            <>
              <button
                type="button"
                onClick={() =>
                  update.mutate(
                    { serverId: server.id, body: { enabled: !server.enabled } },
                    { onError },
                  )
                }
                className="cursor-pointer text-neutral-400 hover:text-neutral-200"
              >
                {server.enabled ? "Disable" : "Enable"}
              </button>
              <button
                type="button"
                disabled={remove.isPending}
                onClick={() => {
                  // Snapshots keep the binding: the next run fails resolution
                  // honestly (D37) — say so in the confirm.
                  if (
                    !window.confirm(
                      `Remove MCP server ${server.name}? Agents bound to its tools will fail tool resolution on their next run.`,
                    )
                  )
                    return;
                  remove.mutate(server.id, { onError });
                }}
                className="cursor-pointer text-red-400 hover:text-red-300 disabled:cursor-not-allowed disabled:text-neutral-600"
              >
                Remove
              </button>
            </>
          )}
        </div>
      </div>
      {showTools && <McpTools serverId={server.id} />}
    </li>
  );
}

const inputClass =
  "w-full rounded border border-neutral-700 bg-neutral-950 px-2 py-1 text-sm text-neutral-100";

function AddServerForm() {
  const create = useCreateMcpServer();
  const [show, setShow] = useState(false);
  const [name, setName] = useState("");
  const [transport, setTransport] = useState<"stdio" | "http">("stdio");
  const [command, setCommand] = useState("");
  const [args, setArgs] = useState("");
  const [url, setUrl] = useState("");

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (name.trim() === "") return; // the API rejects a blank name — never send it
    const config =
      transport === "stdio"
        ? {
            type: "stdio" as const,
            command: command.trim(),
            args: args
              .split(",")
              .map((a) => a.trim())
              .filter((a) => a !== ""),
          }
        : { type: "http" as const, url: url.trim() };
    create.mutate(
      { name: name.trim(), config, enabled: true },
      {
        onSuccess: () => {
          setShow(false);
          setName("");
          setCommand("");
          setArgs("");
          setUrl("");
          toast("success", `Added MCP server ${name.trim()}`);
        },
        onError: (err) => toast("error", err.message),
      },
    );
  };

  if (!show) {
    return (
      <button
        type="button"
        onClick={() => setShow(true)}
        className="cursor-pointer rounded bg-neutral-100 px-3 py-1.5 text-sm font-medium text-neutral-900 hover:bg-white"
      >
        Add server
      </button>
    );
  }

  return (
    <form
      onSubmit={submit}
      className="mt-4 grid max-w-md grid-cols-2 gap-3 rounded border border-neutral-800 bg-neutral-900 p-4"
    >
      <label className="text-sm">
        <span className="text-neutral-400">Name (slug)</span>
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="fixtures"
          className={`mt-1 ${inputClass}`}
        />
      </label>
      <label className="text-sm">
        <span className="text-neutral-400">Transport</span>
        <select
          value={transport}
          onChange={(e) => setTransport(e.target.value as "stdio" | "http")}
          className={`mt-1 ${inputClass}`}
        >
          <option value="stdio">stdio</option>
          <option value="http">http</option>
        </select>
      </label>
      {transport === "stdio" ? (
        <>
          <label className="text-sm">
            <span className="text-neutral-400">Command</span>
            <input
              value={command}
              onChange={(e) => setCommand(e.target.value)}
              placeholder="uvx"
              className={`mt-1 ${inputClass}`}
            />
          </label>
          <label className="text-sm">
            <span className="text-neutral-400">Arguments (comma-separated)</span>
            <input
              value={args}
              onChange={(e) => setArgs(e.target.value)}
              placeholder="mcp-server-time, --local"
              className={`mt-1 ${inputClass}`}
            />
          </label>
        </>
      ) : (
        <label className="col-span-2 text-sm">
          <span className="text-neutral-400">URL</span>
          <input
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            placeholder="https://…"
            className={`mt-1 ${inputClass}`}
          />
        </label>
      )}
      <button
        type="submit"
        disabled={create.isPending}
        className="cursor-pointer justify-self-start rounded bg-neutral-100 px-3 py-1.5 text-sm font-medium text-neutral-900 hover:bg-white disabled:cursor-not-allowed disabled:text-neutral-500"
      >
        {create.isPending ? "Adding…" : "Create server"}
      </button>
      <button
        type="button"
        onClick={() => setShow(false)}
        className="cursor-pointer justify-self-start rounded border border-neutral-700 px-3 py-1.5 text-sm text-neutral-300 hover:bg-neutral-950"
      >
        Cancel
      </button>
      {create.isError && (
        <p className="col-span-2 text-sm text-red-400" role="alert">
          {create.error.message}
        </p>
      )}
    </form>
  );
}

/** MCP server management (S4): admin/owner writes server-side; a member's
 * 403 is shown verbatim if it ever comes back (rule 6 — never pre-gated
 * beyond the whoami-driven hiding of the mutating controls). */
function McpPanel() {
  const { data: whoami } = useWhoami();
  const { data: servers, isPending, isError, error } = useMcpServers();
  // Anonymous mode is the default tenant's full access (the API's own rule).
  const canManage = whoami === null || whoami?.role === "owner" || whoami?.role === "admin";

  return (
    <section className="mt-10" aria-label="MCP servers">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold">MCP servers</h2>
        {canManage && <AddServerForm />}
      </div>
      <p className="mt-1 text-sm text-neutral-400">
        Model Context Protocol servers — agents bind their tools by name
        (<code className="font-mono">mcp__server__tool</code>). Every discovered
        tool requires approval by default.
      </p>

      {isPending ? (
        <p className="mt-4 text-sm text-neutral-400">Loading servers…</p>
      ) : isError ? (
        <p className="mt-4 text-sm text-red-400" role="alert">{error.message}</p>
      ) : servers.length === 0 ? (
        <p className="mt-4 text-sm text-neutral-400">No MCP servers configured.</p>
      ) : (
        <ul className="mt-4 flex flex-col gap-2">
          {servers.map((server) => (
            <McpServerRow key={server.id} server={server} canManage={canManage} />
          ))}
        </ul>
      )}
    </section>
  );
}

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
              {Object.keys(tool.parameters ?? {}).length > 0 && (
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

      {mcp.enabled ? (
        <McpPanel />
      ) : (
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