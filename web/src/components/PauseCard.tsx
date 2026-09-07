import { useState } from "react";

import type { PendingCallView } from "@/stores/runConsole";

// The S10 pause card (ADR 0010/0011): a question to answer, or gated tool
// calls to approve/refuse. Shared by the live run console and the execution
// detail page — two sections, one stable contract. Per-call verdicts are
// STAGED locally; Submit posts the whole map — silence never approves (D33).

interface PauseCardProps {
  question: string | null;
  pendingCalls: PendingCallView[];
  /** A resume POST is in flight — inputs and buttons are disabled. */
  busy: boolean;
  onSubmit: (body: { content?: string; decisions?: Record<string, boolean> }) => void;
}

export function PauseCard({ question, pendingCalls, busy, onSubmit }: PauseCardProps) {
  const [decisions, setDecisions] = useState<Record<string, boolean>>({});
  const [answer, setAnswer] = useState("");

  return (
    <div className="rounded border border-violet-800 bg-violet-950/40 p-4" data-testid="pause-card">
      <p className="text-xs text-violet-300">Waiting for your input{busy ? " — resuming…" : ""}</p>
      {question !== null && (
        <p className="mt-1 whitespace-pre-wrap text-sm text-neutral-100">{question}</p>
      )}
      {pendingCalls.length > 0 && (
        <>
          <ul className="mt-2 space-y-2">
            {pendingCalls.map((call) => {
              const chosen = decisions[call.id];
              return (
                <li
                  key={call.id}
                  className="flex items-center justify-between gap-3 rounded border border-neutral-800 bg-neutral-900 px-3 py-2"
                  data-testid={`pending-call-${call.id}`}
                >
                  <span className="min-w-0 text-sm text-neutral-200">
                    <span className="font-medium">{call.name}</span>{" "}
                    <span className="break-all font-mono text-xs text-neutral-400">
                      {JSON.stringify(call.arguments)}
                    </span>
                  </span>
                  <span className="flex shrink-0 gap-2">
                    <button
                      type="button"
                      aria-pressed={chosen === true}
                      disabled={busy}
                      onClick={() =>
                        setDecisions((d) => ({ ...d, [call.id]: true }))
                      }
                      className={
                        chosen === true
                          ? "cursor-pointer rounded border border-green-500 bg-green-900 px-3 py-1 text-xs font-medium text-green-200 disabled:cursor-not-allowed disabled:text-neutral-600"
                          : "cursor-pointer rounded border border-green-800 px-3 py-1 text-xs text-green-300 hover:bg-green-950 disabled:cursor-not-allowed disabled:text-neutral-600"
                      }
                    >
                      Approve
                    </button>
                    <button
                      type="button"
                      aria-pressed={chosen === false}
                      disabled={busy}
                      onClick={() =>
                        setDecisions((d) => ({ ...d, [call.id]: false }))
                      }
                      className={
                        chosen === false
                          ? "cursor-pointer rounded border border-red-500 bg-red-900 px-3 py-1 text-xs font-medium text-red-200 disabled:cursor-not-allowed disabled:text-neutral-600"
                          : "cursor-pointer rounded border border-red-800 px-3 py-1 text-xs text-red-300 hover:bg-red-950 disabled:cursor-not-allowed disabled:text-neutral-600"
                      }
                    >
                      Reject
                    </button>
                  </span>
                </li>
              );
            })}
          </ul>
          <div className="mt-3 flex items-center justify-between gap-3">
            <button
              type="button"
              data-testid="allow-all"
              disabled={busy}
              onClick={() =>
                setDecisions(
                  Object.fromEntries(pendingCalls.map((c) => [c.id, true])),
                )
              }
              className="cursor-pointer rounded border border-neutral-700 px-3 py-1 text-xs text-neutral-300 hover:bg-neutral-900 disabled:cursor-not-allowed disabled:text-neutral-600"
            >
              Allow all
            </button>
            <button
              type="button"
              data-testid="submit-decisions"
              disabled={
                busy ||
                pendingCalls.some((c) => decisions[c.id] === undefined)
              }
              onClick={() => onSubmit({ decisions })}
              className="cursor-pointer rounded bg-neutral-100 px-4 py-1.5 text-sm font-medium text-neutral-900 hover:bg-white disabled:cursor-not-allowed disabled:text-neutral-500"
            >
              Submit decision
            </button>
          </div>
        </>
      )}
      {pendingCalls.length === 0 && question !== null && (
        <form
          className="mt-3 flex gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            if (answer.trim() === "") return;
            onSubmit({ content: answer });
          }}
        >
          <input
            aria-label="Your answer"
            className="min-w-0 flex-1 rounded border border-neutral-700 bg-neutral-900 px-2 py-1.5 text-sm text-neutral-100 focus:border-neutral-500 focus:outline-none"
            value={answer}
            disabled={busy}
            onChange={(e) => setAnswer(e.target.value)}
            placeholder="Your answer…"
          />
          <button
            type="submit"
            disabled={busy || answer.trim() === ""}
            className="cursor-pointer rounded bg-neutral-100 px-4 py-1.5 text-sm font-medium text-neutral-900 hover:bg-white disabled:cursor-not-allowed disabled:text-neutral-500"
          >
            Send answer
          </button>
        </form>
      )}
    </div>
  );
}