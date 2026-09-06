import { useState } from "react";
import type { FormEvent } from "react";

import {
  useCreateMember,
  useDeleteMember,
  useMembers,
  useUpdateMember,
} from "@/api/queries/settings";
import type { MemberOut } from "@/api/queries/settings";
import { toast } from "@/stores/toast";

// Tenant members (S2, ADR 0009 §8) — admin/owner only server-side; a 403
// here is the API's own answer and is shown verbatim, never pre-gated.

function MemberRow({ member, canManage }: { member: MemberOut; canManage: boolean }) {
  const update = useUpdateMember();
  const remove = useDeleteMember();
  const [editing, setEditing] = useState(false);
  const [displayName, setDisplayName] = useState(member.display_name);

  const patch = (body: { display_name?: string; role?: string }) =>
    update.mutate(
      { userId: member.id, body },
      { onError: (err) => toast("error", err.message) },
    );

  return (
    <tr className="border-b border-neutral-900">
      <td className="py-2 pr-4 text-neutral-100">{member.email}</td>
      <td className="py-2 pr-4">
        {editing ? (
          <input
            value={displayName}
            onChange={(e) => setDisplayName(e.target.value)}
            aria-label={`Display name for ${member.email}`}
            className="rounded border border-neutral-700 bg-neutral-900 px-2 py-0.5 text-sm"
          />
        ) : (
          <span className="text-neutral-400">{member.display_name}</span>
        )}
      </td>
      <td className="py-2 pr-4">
        {editing ? (
          <select
            value={member.role}
            onChange={(e) => {
              patch({ role: e.target.value });
              setEditing(false);
            }}
            className="rounded border border-neutral-700 bg-neutral-900 px-2 py-0.5 text-sm"
          >
            {["owner", "admin", "member"].map((role) => (
              <option key={role} value={role}>
                {role}
              </option>
            ))}
          </select>
        ) : (
          <span className="font-mono text-xs text-neutral-300">{member.role}</span>
        )}
      </td>
      <td className="py-2 pr-4 text-right">
        {editing ? (
          <>
            <button
              type="button"
              onClick={() => {
                patch({ display_name: displayName });
                setEditing(false);
              }}
              className="cursor-pointer text-neutral-300 hover:text-neutral-100"
            >
              save
            </button>
            <button
              type="button"
              onClick={() => setEditing(false)}
              className="ml-3 cursor-pointer text-neutral-500 hover:text-neutral-300"
            >
              cancel
            </button>
          </>
        ) : canManage ? (
          <>
            <button
              type="button"
              onClick={() => setEditing(true)}
              className="cursor-pointer text-neutral-400 hover:text-neutral-200"
            >
              edit
            </button>
            <button
              type="button"
              disabled={remove.isPending}
              onClick={() => {
                if (!window.confirm(`Remove ${member.email} from the tenant?`)) return;
                remove.mutate(member.id, {
                  onError: (err) => toast("error", err.message),
                });
              }}
              className="ml-3 cursor-pointer text-red-400 hover:text-red-300 disabled:cursor-not-allowed disabled:text-neutral-600"
            >
              remove
            </button>
          </>
        ) : null}
      </td>
    </tr>
  );
}

export function MembersPanel({ canManage }: { canManage: boolean }) {
  const { data: members, isPending, isError, error } = useMembers();
  const create = useCreateMember();
  const [showForm, setShowForm] = useState(false);
  const [email, setEmail] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [role, setRole] = useState("member");
  const [password, setPassword] = useState("");

  const submit = (event: FormEvent) => {
    event.preventDefault();
    create.mutate(
      {
        email,
        display_name: displayName,
        role,
        // Omitted when blank — a keys-only member (backend schema optionality).
        ...(password !== "" ? { password } : {}),
      },
      {
        onSuccess: () => {
          setShowForm(false);
          setEmail("");
          setDisplayName("");
          setPassword("");
          toast("success", `Invited ${email}`);
        },
        onError: (err) => toast("error", err.message),
      },
    );
  };

  return (
    <section className="mt-10" aria-label="Members">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold">Members</h2>
        {canManage && (
          <button
            type="button"
            onClick={() => setShowForm((v) => !v)}
            className="cursor-pointer rounded bg-neutral-100 px-3 py-1.5 text-sm font-medium text-neutral-900 hover:bg-white"
          >
            {showForm ? "Cancel" : "Invite member"}
          </button>
        )}
      </div>

      {showForm && (
        <form
          onSubmit={submit}
          className="mt-4 grid max-w-md grid-cols-2 gap-3 rounded border border-neutral-800 bg-neutral-900 p-4"
        >
          <label className="text-sm">
            <span className="text-neutral-400">Email</span>
            <input
              type="email"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className="mt-1 w-full rounded border border-neutral-700 bg-neutral-950 px-2 py-1 text-sm"
            />
          </label>
          <label className="text-sm">
            <span className="text-neutral-400">Display name</span>
            <input
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
              className="mt-1 w-full rounded border border-neutral-700 bg-neutral-950 px-2 py-1 text-sm"
            />
          </label>
          <label className="text-sm">
            <span className="text-neutral-400">Role</span>
            <select
              value={role}
              onChange={(e) => setRole(e.target.value)}
              className="mt-1 w-full rounded border border-neutral-700 bg-neutral-950 px-2 py-1 text-sm"
            >
              {["member", "admin", "owner"].map((r) => (
                <option key={r} value={r}>
                  {r}
                </option>
              ))}
            </select>
          </label>
          <label className="text-sm">
            <span className="text-neutral-400">Password (optional)</span>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="keys-only member if blank"
              className="mt-1 w-full rounded border border-neutral-700 bg-neutral-950 px-2 py-1 text-sm"
            />
          </label>
          <button
            type="submit"
            disabled={create.isPending}
            className="cursor-pointer justify-self-start rounded bg-neutral-100 px-3 py-1.5 text-sm font-medium text-neutral-900 hover:bg-white disabled:cursor-not-allowed disabled:text-neutral-500"
          >
            {create.isPending ? "Inviting…" : "Create member"}
          </button>
          {create.isError && (
            <p className="text-sm text-red-400" role="alert">
              {create.error.message}
            </p>
          )}
        </form>
      )}

      {isPending ? (
        <p className="mt-4 text-sm text-neutral-400">Loading members…</p>
      ) : isError ? (
        <p className="mt-4 text-sm text-red-400">{error.message}</p>
      ) : members.length === 0 ? (
        <p className="mt-4 text-sm text-neutral-400">No members listed.</p>
      ) : (
        <table className="mt-4 w-full text-left text-sm">
          <thead className="text-neutral-400">
            <tr className="border-b border-neutral-800">
              <th className="py-2 pr-4 font-medium">Email</th>
              <th className="py-2 pr-4 font-medium">Display name</th>
              <th className="py-2 pr-4 font-medium">Role</th>
              <th className="py-2 pr-4 text-right font-medium">Actions</th>
            </tr>
          </thead>
          <tbody>
            {members.map((member) => (
              <MemberRow key={member.id} member={member} canManage={canManage} />
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}