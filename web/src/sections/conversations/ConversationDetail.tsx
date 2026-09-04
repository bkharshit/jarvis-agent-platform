import { Link, useParams } from "react-router";

import { useConversationMessages } from "@/api/queries/conversations";
import { contentToText } from "@/components/messageContent";
import { SectionGate } from "@/capabilities/SectionGate";

// Per-session transcript from the real conversations endpoint. A 404 (no
// conversation for this agent/session pair) renders an honest empty state.

function ConversationDetailInner() {
  const { agentId, sessionId } = useParams();
  const { data, isPending, isError, error } = useConversationMessages(agentId, sessionId);

  if (isPending) {
    return <p className="px-6 py-10 text-sm text-neutral-400">Loading transcript…</p>;
  }
  if (isError) {
    return (
      <div className="mx-auto max-w-3xl px-6 py-10">
        <p className="text-sm text-red-400">{error.message}</p>
        <p className="mt-2 text-sm text-neutral-400">
          No conversation exists for this agent and session pair.
        </p>
        <Link to="/conversations" className="mt-4 inline-block text-sm text-neutral-300 underline">
          ← All conversations
        </Link>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-3xl px-6 py-8">
      <h1 className="font-mono text-lg font-semibold">{data.session_id}</h1>
      <p className="mt-1 font-mono text-xs text-neutral-400">agent {data.agent_id}</p>

      {data.messages.length === 0 ? (
        <p className="mt-10 text-sm text-neutral-400">This conversation has no messages.</p>
      ) : (
        <ul className="mt-6 flex flex-col gap-2">
          {data.messages.map((m, i) => (
            <li key={i} className="rounded bg-neutral-900 p-3 text-sm">
              <span className="text-xs text-neutral-400">
                {m.role}
                {m.role === "tool" && m.name ? ` · ${m.name}` : ""}
              </span>
              <p className="mt-1 whitespace-pre-wrap text-neutral-200">
                {contentToText(m.content)}
              </p>
            </li>
          ))}
        </ul>
      )}

      <Link to="/conversations" className="mt-8 inline-block text-sm text-neutral-400 underline">
        ← All conversations
      </Link>
    </div>
  );
}

export function ConversationDetailPage() {
  return (
    <SectionGate sectionKey="conversations">
      <ConversationDetailInner />
    </SectionGate>
  );
}