import { useQuery } from "@tanstack/react-query";

import { client, unwrap } from "@/api/client";

import type { components } from "@/api/schema";

export type RunResult = components["schemas"]["RunResult"];
export type ExecutionDetail = components["schemas"]["ExecutionDetail"];
export type EventList = components["schemas"]["EventList"];

/** The backend's ExecutionStatus — generated, never hand-written. */
export type ExecutionStatus = RunResult["status"];

export function executionsQueryKey(filters: {
  agent?: string | null;
  status?: ExecutionStatus | null;
}) {
  return ["executions", { agent: filters.agent ?? null, status: filters.status ?? null }] as const;
}

export function executionQueryKey(runId: string) {
  return ["executions", runId] as const;
}

export function useExecutions(filters: {
  agent?: string | null;
  status?: ExecutionStatus | null;
}) {
  return useQuery({
    queryKey: executionsQueryKey(filters),
    queryFn: async () => {
      const body = await unwrap(
        client.GET("/v1/executions", {
          params: {
            query: {
              ...(filters.agent ? { agent_id: filters.agent } : {}),
              ...(filters.status ? { status: filters.status } : {}),
            },
          },
        }),
      );
      return body.items;
    },
  });
}

export function useExecution(runId: string | undefined) {
  return useQuery({
    queryKey: executionQueryKey(runId ?? ""),
    enabled: runId !== undefined,
    queryFn: () =>
      unwrap(client.GET("/v1/executions/{run_id}", { params: { path: { run_id: runId! } } })),
  });
}

/** JSON replay (CursorEvent cursor space) — feeds the same applyEvent reducer as live SSE. */
export function useReplayEvents(runId: string | undefined) {
  return useQuery({
    queryKey: [...executionQueryKey(runId ?? ""), "events"],
    enabled: runId !== undefined,
    queryFn: () =>
      unwrap(
        client.GET("/v1/executions/{run_id}/events", { params: { path: { run_id: runId! } } }),
      ),
  });
}

/** ADR 0014 debug trace: the actual model request/response per call, from
 * the backend's process-local buffer. Polls while the run is live so trace
 * entries appear as iterations happen; empty when the flag is off, the
 * backend restarted, or a separate worker executed the run. `enabled`
 * follows the capabilities flag so the section can render null without a
 * conditional hook. */
export function useLlmTrace(runId: string, options: { enabled: boolean; live: boolean }) {
  return useQuery({
    queryKey: [...executionQueryKey(runId), "llm-trace"],
    enabled: options.enabled,
    queryFn: () =>
      unwrap(
        client.GET("/v1/executions/{run_id}/llm-trace", { params: { path: { run_id: runId! } } }),
      ),
    refetchInterval: options.live ? 2000 : false,
  });
}