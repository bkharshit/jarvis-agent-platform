import { useQuery } from "@tanstack/react-query";

import { client, unwrap } from "@/api/client";

import type { components } from "@/api/schema";

// Traces (S7, ADR 0021 §7, D77): the spans table's read side — grouped
// summaries newest first, one trace's spans chronological. The trace_id IS
// the run's trace_id, so a multi-agent conversation is one trace.

export type TraceSummary = components["schemas"]["TraceSummary"];
export type TraceDetail = components["schemas"]["TraceDetail"];
export type SpanRecord = components["schemas"]["SpanRecord"];

export const tracesQueryKey = ["traces"] as const;
export const traceQueryKey = (traceId: string) => ["traces", traceId] as const;

export function useTraces() {
  return useQuery({
    queryKey: tracesQueryKey,
    queryFn: async () => {
      const body = await unwrap(client.GET("/v1/traces"));
      return body.traces;
    },
  });
}

export function useTrace(traceId: string) {
  return useQuery({
    queryKey: traceQueryKey(traceId),
    enabled: traceId !== "",
    queryFn: async () =>
      unwrap(client.GET("/v1/traces/{trace_id}", { params: { path: { trace_id: traceId } } })),
  });
}