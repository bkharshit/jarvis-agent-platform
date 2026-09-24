import { useState } from "react";
import type { FormEvent } from "react";
import { Plus, Wrench } from "lucide-react";

import { useCreateMcpServer, useDeleteMcpServer, useMcpServers, useProbeMcpServer, useUpdateMcpServer } from "@/api/queries/mcp";
import type { McpServer } from "@/api/queries/mcp";
import { useCreateCredential, useCredentials } from "@/api/queries/settings";
import type { CredentialOut } from "@/api/queries/settings";
import { PageHeader } from "@/components/catalog/PageHeader";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { EmptyState } from "@/components/ui/EmptyState";
import { Input } from "@/components/ui/Input";
import { builtinToolsFull, mcpGate, settingsFacts } from "@/capabilities/detail";
import { SectionGate } from "@/capabilities/SectionGate";
import { useCapabilities } from "@/capabilities/useCapabilities";
import { useWhoami } from "@/api/queries/auth";
import { toast } from "@/stores/toast";

import { HeaderRefsEditor, headerRefsComplete } from "./HeaderRefsEditor";
import type { HeaderRefs } from "./HeaderRefsEditor";
import { RotateCredentialButton } from "./RotateCredentialButton";

// Tools registry — entirely payload-driven from the capabilities detail
// block. Nothing about the builtin set is hardcoded here. The MCP panel
// (S4) lists the tenant's servers live from the listing route; the
// capability gate only decides panel-vs-coming-soon.

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
        <li
          key={tool.name}
          className="rounded-xl border border-neutral-800/80 bg-neutral-900/50 p-3"
        >
          <h4 className="font-mono text-xs text-neutral-100">{tool.name}</h4>
          <p className="mt-1 text-xs text-neutral-400">{tool.description}</p>
          {Object.keys(tool.parameters ?? {}).length > 0 && (
            <details className="mt-1">
              <summary className="cursor-pointer text-xs text-neutral-500">
                Parameter schema
              </summary>
              <pre className="mt-1 max-h-60 overflow-auto rounded-lg bg-neutral-950 p-2 font-mono text-xs text-neutral-300">
                {JSON.stringify(tool.parameters, null, 2)}
              </pre>
            </details>
          )}
        </li>
      ))}
    </ul>
  );
}

/** Refs on a server row: http rows carry `headers`, stdio rows carry `env` —
 *  both the same CredentialRef map shape (ADR 0013). */
function refsOf(config: McpServer["config"]): HeaderRefs {
  if (config.type === "http") return (config.headers ?? {}) as HeaderRefs;
  return (config.env ?? {}) as HeaderRefs;
}

function EditRefsForm({
  server,
  credentials,
  allowStored,
  onClose,
}: {
  server: McpServer;
  credentials: CredentialOut[];
  allowStored: boolean;
  onClose: () => void;
}) {
  const update = useUpdateMcpServer();
  const createCredential = useCreateCredential();
  const [refs, setRefs] = useState<HeaderRefs>(refsOf(server.config));
  // Enabled only once the shape actually differs from the row — an untouched
  // editor never PATCHes.
  const dirty = JSON.stringify(refs) !== JSON.stringify(refsOf(server.config));

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (!dirty || !headerRefsComplete(refs)) return; // drafts never send
    // The patch carries the FULL config — PATCH replaces config wholesale,
    // so refs-only would wipe url/command/args server-side.
    const config =
      server.config.type === "http"
        ? { ...server.config, headers: refs }
        : { ...server.config, env: refs };
    update.mutate(
      { serverId: server.id, body: { config } },
      {
        onSuccess: () => {
          toast("success", `Updated refs for ${server.name}`);
          onClose();
        },
        onError: (err) => toast("error", err.message),
      },
    );
  };

  return (
    <form
      onSubmit={submit}
      className="mt-3 rounded-xl border border-neutral-800/80 bg-neutral-950 p-3"
    >
      <p className="mb-2 text-xs text-neutral-500">
        {server.config.type === "http"
          ? "Header refs — secrets are resolved at connect time, never stored here."
          : "Subprocess env refs — values resolve at connect time, never stored here."}
      </p>
      <HeaderRefsEditor
        value={refs}
        onChange={setRefs}
        credentials={credentials}
        allowStored={allowStored}
        onInlineCreate={(credName, secret) =>
          createCredential.mutate(
            { name: credName, provider: "mcp_header", secret },
            {
              onSuccess: (created: CredentialOut) =>
                setRefs((current) => {
                  const next: HeaderRefs = {};
                  for (const [header, ref] of Object.entries(current)) {
                    next[header] =
                      ref.type === "stored" && ref.credential_id === ""
                        ? { type: "stored", credential_id: created.id }
                        : ref;
                  }
                  return next;
                }),
            },
          )
        }
        creatingCredential={createCredential.isPending}
        createError={createCredential.isError ? createCredential.error.message : null}
      />
      <div className="mt-3 flex items-center gap-3">
        <Button type="submit" size="sm" disabled={!dirty || update.isPending}>
          {update.isPending ? "Saving…" : "Save refs"}
        </Button>
        <Button variant="secondary" size="sm" onClick={onClose}>
          Cancel
        </Button>
      </div>
      {update.isError && (
        <p className="mt-2 text-xs text-red-400" role="alert">
          {update.error.message}
        </p>
      )}
    </form>
  );
}

function McpServerRow({
  server,
  canManage,
  credentials,
  allowStored,
}: {
  server: McpServer;
  canManage: boolean;
  credentials: CredentialOut[];
  allowStored: boolean;
}) {
  const update = useUpdateMcpServer();
  const remove = useDeleteMcpServer();
  const [showTools, setShowTools] = useState(false);
  const [editing, setEditing] = useState(false);

  const onError = (err: Error) => toast("error", err.message);
  const refs = refsOf(server.config);
  const storedRefs: { header: string; credentialId: string }[] = [];
  for (const [header, ref] of Object.entries(refs)) {
    if (ref.type === "stored") storedRefs.push({ header, credentialId: ref.credential_id });
  }
  const credentialName = (id: string) =>
    credentials.find((c) => c.id === id)?.name ?? id;

  return (
    <li className="rounded-xl border border-neutral-800/80 bg-neutral-900/50 p-4 transition-all hover:border-neutral-700/80">
      <div className="flex items-center justify-between gap-3">
        <div className="min-w-0">
          <h3 className="font-mono text-sm text-neutral-100">{server.name}</h3>
          <p className="mt-1 flex items-center gap-2 text-xs text-neutral-400">
            <Badge variant="neutral" className="font-mono">{server.config.type}</Badge>
            {server.enabled ? (
              <Badge variant="success" dot>enabled</Badge>
            ) : (
              <span className="text-neutral-500">disabled</span>
            )}
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-3 text-xs">
          <Button variant="ghost" size="sm" onClick={() => setShowTools((v) => !v)}>
            {showTools ? "Hide tools" : "View tools"}
          </Button>
          {canManage && (
            <>
              <Button variant="ghost" size="sm" onClick={() => setEditing((v) => !v)}>
                {editing
                  ? "Hide editor"
                  : server.config.type === "http"
                    ? "Edit headers"
                    : "Edit env"}
              </Button>
              <Button
                variant="ghost"
                size="sm"
                onClick={() =>
                  update.mutate(
                    { serverId: server.id, body: { enabled: !server.enabled } },
                    { onError },
                  )
                }
              >
                {server.enabled ? "Disable" : "Enable"}
              </Button>
              <Button
                variant="ghost"
                size="sm"
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
                className="text-red-400 hover:bg-rose-950/40 hover:text-red-300"
              >
                Remove
              </Button>
            </>
          )}
        </div>
      </div>
      {canManage && storedRefs.length > 0 && (
        <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-neutral-500">
          <span>Stored credential refs:</span>
          {storedRefs.map((r) => (
            <span key={r.header} className="flex items-center gap-2">
              <span className="font-mono text-neutral-400">{r.header}</span>
              <span>→ {credentialName(r.credentialId)}</span>
              <RotateCredentialButton
                credentialId={r.credentialId}
                credentialName={credentialName(r.credentialId)}
              />
            </span>
          ))}
        </div>
      )}
      {editing && canManage && (
        <EditRefsForm
          server={server}
          credentials={credentials}
          allowStored={allowStored}
          onClose={() => setEditing(false)}
        />
      )}
      {showTools && <McpTools serverId={server.id} />}
    </li>
  );
}

function AddServerForm({ allowStored }: { allowStored: boolean }) {
  const create = useCreateMcpServer();
  const { data: credentials } = useCredentials();
  const createCredential = useCreateCredential();
  const [show, setShow] = useState(false);
  const [name, setName] = useState("");
  const [transport, setTransport] = useState<"stdio" | "http">("stdio");
  const [command, setCommand] = useState("");
  const [args, setArgs] = useState("");
  const [url, setUrl] = useState("");
  const [headers, setHeaders] = useState<HeaderRefs>({});

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (name.trim() === "") return; // the API rejects a blank name — never send it
    if (!headerRefsComplete(headers)) return; // blank header drafts never send
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
        : {
            type: "http" as const,
            url: url.trim(),
            // refs only — the secret went to /v1/credentials, never here
            ...(Object.keys(headers).length > 0 ? { headers } : {}),
          };
    create.mutate(
      { name: name.trim(), config, enabled: true },
      {
        onSuccess: () => {
          setShow(false);
          setName("");
          setCommand("");
          setArgs("");
          setUrl("");
          setHeaders({});
          toast("success", `Added MCP server ${name.trim()}`);
        },
        onError: (err) => toast("error", err.message),
      },
    );
  };

  if (!show) {
    return (
      <Button size="sm" icon={<Plus aria-hidden />} onClick={() => setShow(true)}>
        Add server
      </Button>
    );
  }

  return (
    <form
      onSubmit={submit}
      className="mt-4 grid max-w-md grid-cols-2 gap-3 rounded-xl border border-neutral-800/80 bg-neutral-900/50 p-4"
    >
      <label className="text-sm">
        <span className="text-neutral-400">Name (slug)</span>
        <Input
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="fixtures"
          className="mt-1"
        />
      </label>
      <label className="text-sm">
        <span className="text-neutral-400">Transport</span>
        <select
          value={transport}
          onChange={(e) => setTransport(e.target.value as "stdio" | "http")}
          className="mt-1 w-full cursor-pointer rounded-md border border-neutral-800 bg-neutral-900/70 px-3 py-1.5 text-sm text-neutral-100 transition-colors focus:border-indigo-500 focus:outline-none focus:ring-2 focus:ring-indigo-500/30"
        >
          <option value="stdio">stdio</option>
          <option value="http">http</option>
        </select>
      </label>
      {transport === "stdio" ? (
        <>
          <label className="text-sm">
            <span className="text-neutral-400">Command</span>
            <Input
              value={command}
              onChange={(e) => setCommand(e.target.value)}
              placeholder="uvx"
              className="mt-1"
            />
          </label>
          <label className="text-sm">
            <span className="text-neutral-400">Arguments (comma-separated)</span>
            <Input
              value={args}
              onChange={(e) => setArgs(e.target.value)}
              placeholder="mcp-server-time, --local"
              className="mt-1"
            />
          </label>
        </>
      ) : (
        <label className="col-span-2 text-sm">
          <span className="text-neutral-400">URL</span>
          <Input
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            placeholder="https://…"
            className="mt-1"
          />
        </label>
      )}
      {transport === "http" && (
        <div className="col-span-2 text-sm">
          <span className="text-neutral-400">
            Headers (values are references — secrets are sent once, encrypted server-side)
          </span>
          <div className="mt-2">
            <HeaderRefsEditor
              value={headers}
              onChange={setHeaders}
              credentials={credentials ?? []}
              allowStored={allowStored}
              onInlineCreate={(credName, secret) =>
                createCredential.mutate(
                  { name: credName, provider: "mcp_header", secret },
                  {
                    onSuccess: (created: CredentialOut) =>
                      setHeaders((current) => {
                        const next: HeaderRefs = {};
                        for (const [header, ref] of Object.entries(current)) {
                          next[header] =
                            ref.type === "stored" && ref.credential_id === ""
                              ? { type: "stored", credential_id: created.id }
                              : ref;
                        }
                        return next;
                      }),
                  },
                )
              }
              creatingCredential={createCredential.isPending}
              createError={createCredential.isError ? createCredential.error.message : null}
            />
          </div>
        </div>
      )}
      <Button type="submit" size="sm" className="justify-self-start" disabled={create.isPending}>
        {create.isPending ? "Adding…" : "Create server"}
      </Button>
      <Button
        variant="secondary"
        size="sm"
        className="justify-self-start"
        onClick={() => setShow(false)}
      >
        Cancel
      </Button>
      {create.isError && (
        <p className="col-span-2 text-sm text-red-400" role="alert">
          {create.error.message}
        </p>
      )}
    </form>
  );
}

/** MCP server management (S4): admin/owner writes server-side; a member's
 *  403 is shown verbatim if it ever comes back (rule 6 — never pre-gated
 *  beyond the whoami-driven hiding of the mutating controls). */
function McpPanel() {
  const { data: whoami } = useWhoami();
  const { data: capabilities } = useCapabilities();
  const { data: servers, isPending, isError, error } = useMcpServers();
  const { data: credentials } = useCredentials();
  // Anonymous mode is the default tenant's full access (the API's own rule):
  // the whoami route answers 200 with mode:"anonymous", role:null — NOT a
  // null whoami. Only a signed-in member loses the mutating controls.
  const canManage =
    whoami != null &&
    (whoami.mode === "anonymous" ||
      whoami.role === "owner" ||
      whoami.role === "admin");
  // Stored-credential headers (ADR 0013) need a signed-in principal (the
  // create route 403s anonymous) AND the master key configured — otherwise
  // the option is absent from the form, never shown-disabled (rule 6).
  const allowStored =
    whoami != null &&
    whoami.mode !== "anonymous" &&
    settingsFacts(capabilities).credentialsAvailable;

  return (
    <section className="mt-10" aria-label="MCP servers">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold">MCP servers</h2>
        {canManage && <AddServerForm allowStored={allowStored} />}
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
        <EmptyState
          icon={<Wrench className="h-6 w-6" />}
          title="No MCP servers configured"
          description="Add a server — agents bind discovered tools by name, and every tool requires approval by default."
        />
      ) : (
        <ul className="mt-4 flex flex-col gap-2">
          {servers.map((server) => (
            <McpServerRow
              key={server.id}
              server={server}
              canManage={canManage}
              credentials={credentials ?? []}
              allowStored={allowStored}
            />
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
  const [search, setSearch] = useState("");

  const needle = search.trim().toLowerCase();
  const visible =
    needle === ""
      ? tools
      : tools.filter(
          (tool) =>
            tool.name.toLowerCase().includes(needle) ||
            tool.description.toLowerCase().includes(needle),
        );

  return (
    <div className="mx-auto max-w-4xl px-6 py-8">
      <PageHeader
        title="Tools"
        description="Builtin registry from the backend — agents bind these by name."
        search={{
          value: search,
          onChange: setSearch,
          placeholder: "Search tools…",
        }}
      />

      {tools.length === 0 ? (
        <EmptyState
          icon={<Wrench className="h-6 w-6" />}
          title="No tools registered."
          description="The backend reports an empty builtin registry."
        />
      ) : visible.length === 0 ? (
        <EmptyState
          icon={<Wrench className="h-6 w-6" />}
          title="No tools match"
          description="Nothing matches this search — try a different term."
        />
      ) : (
        <ul className="mt-6 flex flex-col gap-2">
          {visible.map((tool) => (
            <li
              key={tool.name}
              className="rounded-xl border border-neutral-800/80 bg-neutral-900/50 p-4 transition-all hover:border-neutral-700/80"
            >
              <div className="flex items-start justify-between gap-3">
                <h2 className="font-mono text-sm text-neutral-100">{tool.name}</h2>
                {Object.keys(tool.parameters ?? {}).length > 0 && (
                  <Badge variant="neutral">
                    {Object.keys(tool.parameters ?? {}).length} params
                  </Badge>
                )}
              </div>
              <p className="mt-1 text-sm text-neutral-400">{tool.description}</p>
              {Object.keys(tool.parameters ?? {}).length > 0 && (
                <details className="mt-2">
                  <summary className="cursor-pointer text-xs text-neutral-500">
                    Parameter schema
                  </summary>
                  <pre className="mt-2 max-h-60 overflow-auto rounded-lg bg-neutral-950 p-2 font-mono text-xs text-neutral-300">
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
        <p className="mt-8 rounded-xl border border-neutral-800/80 p-4 text-sm text-neutral-400">
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