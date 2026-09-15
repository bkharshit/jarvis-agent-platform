import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { client, unwrap } from "@/api/client";
import { apiErrorFromResponse } from "@/api/errors";

import type { components } from "@/api/schema";

// Evaluations (S11, ADR 0017 §6): datasets CRUD, eval runs over the
// ordinary queue (202 — the runs happen after this process returns),
// run detail with derived status + lazily-scored results, version
// comparison. Status is a backend DERIVED fact (D49) — poll while
// "running"; scoring happens server-side on the first completed read.

export type EvalDataset = components["schemas"]["EvalDataset"];
export type EvalDatasetUpsert = components["schemas"]["EvalDatasetUpsert"];
export type EvalRunSummary = components["schemas"]["EvalRunSummary"];
export type EvalRunDetail = components["schemas"]["EvalRunDetail"];
export type EvalCase = components["schemas"]["EvalCase"];
export type ScorerConfig = components["schemas"]["ScorerConfig"];
export type EvalResult = components["schemas"]["EvalResult"];
export type Score = components["schemas"]["Score"];
export type EvalCompareResponse = components["schemas"]["EvalCompareResponse"];
export type ModelRef = components["schemas"]["ModelRef"];

export const evalDatasetsQueryKey = ["eval-datasets"] as const;
export const evalRunsQueryKey = ["eval-runs"] as const;

export function useEvalDatasets() {
  return useQuery({
    queryKey: evalDatasetsQueryKey,
    queryFn: async () => {
      const body = await unwrap(client.GET("/v1/evaluations/datasets"));
      return body.items;
    },
  });
}

export function useEvalDataset(datasetId: string | undefined) {
  return useQuery({
    queryKey: [...evalDatasetsQueryKey, datasetId],
    enabled: datasetId !== undefined,
    queryFn: async () => {
      const body = await unwrap(
        client.GET("/v1/evaluations/datasets/{dataset_id}", {
          params: { path: { dataset_id: datasetId! } },
        }),
      );
      return body;
    },
  });
}

function invalidateDatasets(queryClient: ReturnType<typeof useQueryClient>) {
  void queryClient.invalidateQueries({ queryKey: evalDatasetsQueryKey });
}

export function useCreateEvalDataset() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: EvalDatasetUpsert) =>
      unwrap(client.POST("/v1/evaluations/datasets", { body })),
    onSuccess: () => invalidateDatasets(queryClient),
  });
}

export function useUpdateEvalDataset(datasetId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: EvalDatasetUpsert) =>
      unwrap(
        client.PATCH("/v1/evaluations/datasets/{dataset_id}", {
          params: { path: { dataset_id: datasetId } },
          body,
        }),
      ),
    onSuccess: () => invalidateDatasets(queryClient),
  });
}

export function useDeleteEvalDataset() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (datasetId: string) => {
      const { response, error } = await client.DELETE(
        "/v1/evaluations/datasets/{dataset_id}",
        { params: { path: { dataset_id: datasetId } } },
      );
      if (!response.ok) throw apiErrorFromResponse(response, error);
    },
    onSuccess: () => invalidateDatasets(queryClient),
  });
}

export function useCreateEvalRun(datasetId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (agentId: string) =>
      unwrap(
        client.POST("/v1/evaluations/datasets/{dataset_id}/runs", {
          params: { path: { dataset_id: datasetId } },
          body: { agent_id: agentId },
        }),
      ),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: evalRunsQueryKey }),
  });
}

export function useEvalRuns(filters: { agentId?: string; datasetId?: string }) {
  return useQuery({
    queryKey: [evalRunsQueryKey, filters],
    queryFn: async () => {
      const body = await unwrap(
        client.GET("/v1/evaluations/runs", {
          params: {
            query: {
              agent_id: filters.agentId || undefined,
              dataset_id: filters.datasetId || undefined,
            },
          },
        }),
      );
      return body.items;
    },
    // a freshly-created eval run stays "running" until its children finish
    refetchInterval: (query) =>
      query.state.data?.some((run) => run.status === "running") ? 1000 : false,
  });
}

export function useEvalRun(runId: string | undefined) {
  return useQuery({
    queryKey: [...evalRunsQueryKey, runId],
    enabled: runId !== undefined,
    queryFn: async () => {
      const body = await unwrap(
        client.GET("/v1/evaluations/runs/{run_id}", {
          params: { path: { run_id: runId! } },
        }),
      );
      return body;
    },
    // the detail read SCORES lazily on completion (D49) — keep polling
    // until terminal so the first post-completion read happens here
    refetchInterval: (query) =>
      query.state.data?.status === "running" ? 750 : false,
  });
}

export function useEvalCompare(agentId: string | undefined) {
  return useQuery({
    queryKey: ["eval-compare", agentId],
    enabled: agentId !== undefined,
    queryFn: async () => {
      const body = await unwrap(
        client.GET("/v1/evaluations/compare", {
          params: { query: { agent_id: agentId! } },
        }),
      );
      return body;
    },
  });
}