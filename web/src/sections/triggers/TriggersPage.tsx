import { useState } from "react";
import { Link } from "react-router";
import type { FormEvent } from "react";

import { useAgents } from "@/api/queries/agents";
import {
  useCreateTrigger,
  useDeleteTrigger,
  useFireTrigger,
  useRotateTriggerSecret,
  useTriggers,
  useUpdateTrigger,
} from "@/api/queries/triggers";
import type { TriggerOut } from "@/api/queries/triggers";
import { useWhoami } from "@/api/queries/auth";
import { SectionGate } from "@/capabilities/SectionGate";
import { toast } from "@/stores/toast";

// Triggers (S13, ADR 0019 §6, D62): cron / webhook / event rules that fire
// ordinary queue runs. The webhook secret's plaintext exists EXACTLY ONCE
// (create / rotate responses) — it renders once per appearance and never
// from a GET; the webhook URL is derived client-side from window.location.

const KINDS = ["cron", "webhook", "event"] as const;
type Kind = (typeof KINDS)[number];

const inputClass =
  "w-full rounded border border-neutral-700 bg-neutral-950 px-2 py-1 text-sm text-neutral-100";

const kindBadge: Record<Kind, string> = {
  cron: "bg-sky-950 text-sky-300",
  webhook: "bg-amber-950 text-amber-300",
  event: "bg-violet-950 text-violet-300",
};

/** Display-once state for a secret plaintext (create / rotate). */
function SecretOnce({ secret, title }: { secret: string; title: string }) {
  const [dismissed, setDismissed] = useState(false);
  if (dismissed) return null;
  return (
    <div
      className="mt-2 rounded border border-amber-800 bg-amber-950/40 p-3 text-sm"
      data-testid="secret-once"
    >
      <p className="text-xs font-medium text-amber-300">{title}</p>
      <p className="mt-1 break-all font-mono text-xs text-neutral-100">{secret}</p>
      <p className="mt-1 text-xs text-amber-300/80">
        This is the only time it is shown — copy it now.
      </p>
      <button
        type="button"
        onClick={() => setDismissed(true)}
        className="mt-2 cursor-pointer rounded border border-neutral-700 px-2 py-0.5 text-xs text-neutral-300 hover:bg-neutral-900"
      >
        Dismiss
      </button>
    </div>
  );
}

function WebhookUrl({ trigger }: { trigger: TriggerOut }) {
  const url = `${window.location.origin}/v1/triggers/webhook/${trigger.id}`;
  const [copied, setCopied] = useState(false);
  return (
    <p className="mt-1 text-xs text-neutral-400">
      <span className="text-neutral-500">POST</span>{" "}
      <code className="break-all font-mono text-neutral-300">{url}</code>{" "}
      <button
        type="button"
        onClick={() => {
          void navigator.clipboard.writeText(url);
          setCopied(true);
        }}
        className="cursor-pointer text-neutral-500 underline hover:text-neutral-300"
      >
        {copied ? "copied" : "copy"}
      </button>
      <span className="text-neutral-500"> — sign the body with HMAC-SHA256 in X-Jarvis-Signature</span>
    </p>
  );
}

function TriggerRow({ trigger, canManage }: { trigger: TriggerOut; canManage: boolean }) {
  const update = useUpdateTrigger();
  const remove = useDeleteTrigger();
  const fire = useFireTrigger();
  const rotate = useRotateTriggerSecret();
  const [rotated, setRotated] = useState<string | null>(null);

  const onError = (err: Error) => toast("error", err.message);
  const patch = (enabled: boolean) =>
    update.mutate(
      {
        triggerId: trigger.id,
        // FULL document (the PATCH discipline) — every field, every time.
        body: {
          name: trigger.name,
          kind: trigger.kind,
          agent_id: trigger.agent_id,
          version_policy: trigger.version_policy,
          input_template: trigger.input_template ?? null,
          enabled,
          config: trigger.config,
        },
      },
      { onError },
    );

  return (
    <li className="rounded border border-neutral-800 bg-neutral-900 p-4" data-testid="trigger-row">
      <div className="flex items-center justify-between gap-3">
        <div className="min-w-0">
          <h3 className="text-sm font-medium text-neutral-100">{trigger.name}</h3>
          <p className="mt-1 flex flex-wrap items-center gap-2 text-xs text-neutral-400">
            <span className={`rounded px-1.5 py-0.5 font-mono ${kindBadge[trigger.kind]}`}>
              {trigger.kind}
            </span>
            <span className="font-mono">{trigger.agent_id.slice(0, 8)}…</span>
            <span className="font-mono">{trigger.version_policy}</span>
            <span className={trigger.enabled ? "text-green-400" : "text-neutral-500"}>
              {trigger.enabled ? "enabled" : "disabled"}
            </span>
          </p>
          {trigger.kind === "cron" && (
            <p className="mt-1 text-xs text-neutral-400">
              <span className="text-neutral-500">schedule</span>{" "}
              <code className="font-mono text-neutral-300">
                {trigger.config.type === "cron" ? trigger.config.schedule : "?"}
              </code>
              {trigger.next_fire_at !== null && trigger.next_fire_at !== undefined && (
                <>
                  {" · next "}
                  {new Date(trigger.next_fire_at).toLocaleString()}
                </>
              )}
            </p>
          )}
          {trigger.kind === "webhook" && <WebhookUrl trigger={trigger} />}
          {trigger.last_fired_at !== null && trigger.last_fired_at !== undefined && (
            <p className="mt-1 text-xs text-neutral-500">
              last fired {new Date(trigger.last_fired_at).toLocaleString()}
            </p>
          )}
          {trigger.last_error !== null && trigger.last_error !== undefined && (
            <p className="mt-1 break-words text-xs text-red-400" role="alert">
              last error: {trigger.last_error}
            </p>
          )}
        </div>
        <div className="flex shrink-0 items-center gap-3 text-xs">
          <Link
            to={`/executions?trigger=${trigger.id}`}
            className="cursor-pointer text-neutral-400 hover:text-neutral-200"
          >
            Runs
          </Link>
          {canManage && (
            <>
              <button
                type="button"
                data-testid="fire-now"
                disabled={fire.isPending}
                onClick={() =>
                  fire.mutate(trigger.id, {
                    onSuccess: () => toast("success", `Fired ${trigger.name} — run queued`),
                    onError,
                  })
                }
                className="cursor-pointer text-neutral-400 hover:text-neutral-200 disabled:cursor-not-allowed disabled:text-neutral-600"
              >
                Fire now
              </button>
              {trigger.kind === "webhook" && (
                <button
                  type="button"
                  disabled={rotate.isPending}
                  onClick={() =>
                    rotate.mutate(trigger.id, {
                      onSuccess: (res) => setRotated(res.secret),
                      onError,
                    })
                  }
                  className="cursor-pointer text-neutral-400 hover:text-neutral-200 disabled:cursor-not-allowed disabled:text-neutral-600"
                >
                  Rotate secret
                </button>
              )}
              <button
                type="button"
                onClick={() => patch(!trigger.enabled)}
                className="cursor-pointer text-neutral-400 hover:text-neutral-200"
              >
                {trigger.enabled ? "Disable" : "Enable"}
              </button>
              <button
                type="button"
                disabled={remove.isPending}
                onClick={() => {
                  // No-FK provenance (D57): past runs keep the trigger_id and
                  // stay filterable — deleting is always allowed.
                  if (!window.confirm(`Delete trigger ${trigger.name}? Its past runs keep the trigger id.`))
                    return;
                  remove.mutate(trigger.id, { onError });
                }}
                className="cursor-pointer text-red-400 hover:text-red-300 disabled:cursor-not-allowed disabled:text-neutral-600"
              >
                Delete
              </button>
            </>
          )}
        </div>
      </div>
      {rotated !== null && <SecretOnce secret={rotated} title="New webhook secret" />}
    </li>
  );
}

function AddTriggerForm({
  onCreated,
}: {
  onCreated: (created: { name: string; secret?: string | null }) => void;
}) {
  const create = useCreateTrigger();
  const { data: agents } = useAgents();
  const [show, setShow] = useState(false);
  const [name, setName] = useState("");
  const [kind, setKind] = useState<Kind>("cron");
  const [agentId, setAgentId] = useState("");
  const [schedule, setSchedule] = useState("0 3 * * *");
  const [eventStatus, setEventStatus] = useState("succeeded");
  const [eventSource, setEventSource] = useState("");
  const [versionPolicy, setVersionPolicy] = useState<"latest" | "pinned">("latest");
  const [inputTemplate, setInputTemplate] = useState("");

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (name.trim() === "" || agentId === "") return; // drafts never send
    const config =
      kind === "cron"
        ? { type: "cron" as const, schedule: schedule.trim() }
        : kind === "webhook"
          ? { type: "webhook" as const }
          : {
              type: "event" as const,
              status: eventStatus as "succeeded",
              ...(eventSource.trim() === "" ? {} : { source_agent_id: eventSource.trim() }),
            };
    create.mutate(
      {
        name: name.trim(),
        kind,
        agent_id: agentId,
        version_policy: versionPolicy,
        input_template: inputTemplate.trim() === "" ? null : inputTemplate.trim(),
        enabled: true,
        config,
      },
      {
        onSuccess: (created) => {
          onCreated({ name: created.trigger.name, secret: created.secret ?? null });
          setShow(false);
          setName("");
          setAgentId("");
          setEventSource("");
          setInputTemplate("");
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
        Add trigger
      </button>
    );
  }

  return (
    <form
      onSubmit={submit}
      className="mt-4 grid max-w-lg grid-cols-2 gap-3 rounded border border-neutral-800 bg-neutral-900 p-4"
    >
      <label className="text-sm">
        <span className="text-neutral-400">Name</span>
        <input value={name} onChange={(e) => setName(e.target.value)} className={`mt-1 ${inputClass}`} />
      </label>
      <label className="text-sm">
        <span className="text-neutral-400">Kind</span>
        <select
          value={kind}
          onChange={(e) => setKind(e.target.value as Kind)}
          className={`mt-1 ${inputClass}`}
        >
          {KINDS.map((k) => (
            <option key={k} value={k}>
              {k}
            </option>
          ))}
        </select>
      </label>
      <label className="text-sm">
        <span className="text-neutral-400">Target agent</span>
        <select
          value={agentId}
          onChange={(e) => setAgentId(e.target.value)}
          className={`mt-1 ${inputClass}`}
        >
          <option value="">— pick an agent —</option>
          {(agents ?? []).map((a) => (
            <option key={a.id} value={a.id}>
              {a.name}
            </option>
          ))}
        </select>
      </label>
      <label className="text-sm">
        <span className="text-neutral-400">Version policy</span>
        <select
          value={versionPolicy}
          onChange={(e) => setVersionPolicy(e.target.value as "latest" | "pinned")}
          className={`mt-1 ${inputClass}`}
        >
          <option value="latest">latest (follows republishes)</option>
          <option value="pinned">pinned (snapshot at save)</option>
        </select>
      </label>
      {kind === "cron" && (
        <label className="text-sm">
          <span className="text-neutral-400">Cron schedule (UTC)</span>
          <input
            value={schedule}
            onChange={(e) => setSchedule(e.target.value)}
            placeholder="0 3 * * *"
            className={`mt-1 ${inputClass}`}
          />
        </label>
      )}
      {kind === "event" && (
        <>
          <label className="text-sm">
            <span className="text-neutral-400">On status</span>
            <select
              value={eventStatus}
              onChange={(e) => setEventStatus(e.target.value)}
              className={`mt-1 ${inputClass}`}
            >
              {["any", "succeeded", "failed", "cancelled", "timed_out"].map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </label>
          <label className="text-sm">
            <span className="text-neutral-400">Source agent (optional)</span>
            <input
              value={eventSource}
              onChange={(e) => setEventSource(e.target.value)}
              placeholder="all agents"
              className={`mt-1 ${inputClass}`}
            />
          </label>
        </>
      )}
      {kind === "webhook" && (
        <p className="col-span-2 text-xs text-neutral-500">
          The ingest URL and HMAC secret are assigned at creation — the secret
          is shown exactly once.
        </p>
      )}
      <label className="col-span-2 text-sm">
        <span className="text-neutral-400">Input template (optional — {'{{now}}'}, {'{{payload}}'}, {'{{status}}'}, {'{{output}}'} vary by kind)</span>
        <textarea
          value={inputTemplate}
          onChange={(e) => setInputTemplate(e.target.value)}
          rows={2}
          className={`mt-1 ${inputClass}`}
        />
      </label>
      <button
        type="submit"
        disabled={create.isPending}
        className="cursor-pointer justify-self-start rounded bg-neutral-100 px-3 py-1.5 text-sm font-medium text-neutral-900 hover:bg-white disabled:cursor-not-allowed disabled:text-neutral-500"
      >
        {create.isPending ? "Creating…" : "Create trigger"}
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

function TriggersInner() {
  const { data: whoami } = useWhoami();
  const { data: triggers, isPending, isError, error } = useTriggers();
  const [createdSecret, setCreatedSecret] = useState<{ name: string; secret: string } | null>(null);
  const canManage =
    whoami != null &&
    (whoami.mode === "anonymous" || whoami.role === "owner" || whoami.role === "admin");

  return (
    <div className="mx-auto max-w-4xl px-6 py-8">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">Triggers</h1>
        {canManage && (
          <AddTriggerForm
            onCreated={(created) => {
              if (typeof created.secret === "string" && created.secret !== "") {
                setCreatedSecret({ name: created.name, secret: created.secret });
              }
              toast("success", `Created trigger ${created.name}`);
            }}
          />
        )}
      </div>
      <p className="mt-1 text-sm text-neutral-400">
        Rules that fire ordinary runs — a schedule (UTC cron), an authenticated
        webhook, or another run's terminal status. Fired runs are ordinary
        executions and appear in Executions.
      </p>

      {isPending ? (
        <p className="mt-10 text-sm text-neutral-400">Loading triggers…</p>
      ) : isError ? (
        <p className="mt-10 text-sm text-red-400" role="alert">
          {error.message}
        </p>
      ) : triggers.length === 0 ? (
        <p className="mt-10 text-sm text-neutral-400">No triggers configured.</p>
      ) : (
        <ul className="mt-6 flex flex-col gap-2">
          {triggers.map((t) => (
            <TriggerRow key={t.id} trigger={t} canManage={canManage} />
          ))}
        </ul>
      )}

      {createdSecret !== null && (
        <SecretOnce
          secret={createdSecret.secret}
          title={`Webhook secret for ${createdSecret.name}`}
        />
      )}
    </div>
  );
}

export function TriggersPage() {
  return (
    <SectionGate sectionKey="triggers">
      <TriggersInner />
    </SectionGate>
  );
}