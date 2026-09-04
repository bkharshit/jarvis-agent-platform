import { describe, expect, it } from "vitest";

import type { RunResult } from "@/api/queries/executions";

import { groupSessions } from "./groupSessions";

function run(over: Partial<RunResult> & { run_id: string }): RunResult {
  return {
    agent_id: "agent-1",
    status: "succeeded",
    input: "hi",
    trace_id: "",
    error: null,
    error_kind: null,
    started_at: "2026-09-04T13:00:00Z",
    event_cursor: 0,
    ...over,
  } as RunResult;
}

describe("groupSessions", () => {
  it("groups runs by (agent_id, session_id) with run counts", () => {
    const { sessions, total } = groupSessions([
      run({ run_id: "r1", session_id: "s1" }),
      run({ run_id: "r2", session_id: "s1" }),
      run({ run_id: "r3", agent_id: "agent-2", session_id: "s1" }),
    ]);
    expect(total).toBe(2);
    expect(sessions).toEqual([
      { agent_id: "agent-1", session_id: "s1", run_count: 2, last_started_at: "2026-09-04T13:00:00Z" },
      { agent_id: "agent-2", session_id: "s1", run_count: 1, last_started_at: "2026-09-04T13:00:00Z" },
    ]);
  });

  it("excludes session-less runs", () => {
    const { sessions, total } = groupSessions([
      run({ run_id: "r1", session_id: null }),
      run({ run_id: "r2", session_id: undefined }),
      run({ run_id: "r3", session_id: "s1" }),
    ]);
    expect(total).toBe(1);
    expect(sessions[0]?.session_id).toBe("s1");
  });

  it("sorts sessions newest-first by last activity", () => {
    const { sessions } = groupSessions([
      run({ run_id: "r1", session_id: "old", started_at: "2026-09-01T00:00:00Z" }),
      run({ run_id: "r2", session_id: "new", started_at: "2026-09-04T00:00:00Z" }),
    ]);
    expect(sessions.map((s) => s.session_id)).toEqual(["new", "old"]);
  });

  it("keeps the latest started_at per session", () => {
    const { sessions } = groupSessions([
      run({ run_id: "r1", session_id: "s1", started_at: "2026-09-04T10:00:00Z" }),
      run({ run_id: "r2", session_id: "s1", started_at: "2026-09-04T09:00:00Z" }),
    ]);
    expect(sessions[0]?.last_started_at).toBe("2026-09-04T10:00:00Z");
  });

  it("caps the page and reports the honest total", () => {
    const runs = Array.from({ length: 60 }, (_, i) =>
      run({ run_id: `r${i}`, session_id: `s${i}` }),
    );
    const { sessions, total } = groupSessions(runs, 50);
    expect(sessions).toHaveLength(50);
    expect(total).toBe(60);
  });
});