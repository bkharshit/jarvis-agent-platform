import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { client, unwrap } from "@/api/client";
import { apiErrorFromResponse } from "@/api/errors";

import type { components } from "@/api/schema";

// All types come from the generated schema — no hand-written shapes here.
export type WorkflowDefinition = components["schemas"]["WorkflowDefinition"];
export type WorkflowNode = components["schemas"]["WorkflowNode"];
export type WorkflowEdge = components["schemas"]["WorkflowEdge"];
export type WorkflowDetail = components["schemas"]["WorkflowDetail"];
export type WorkflowUpsert = components["schemas"]["WorkflowUpsertRequest"];

export const workflowsQueryKey = ["workflows"] as const;
export const workflowQueryKey = (id: string) => ["workflows", id] as const;

export function useWorkflows() {
  return useQuery({
    queryKey: workflowsQueryKey,
    queryFn: async () => {
      const body = await unwrap(client.GET("/v1/workflows"));
      return body.items;
    },
  });
}

/** Definition + versions + publish lints (warnings, never failures). */
export function useWorkflow(id: string | undefined) {
  return useQuery({
    queryKey: workflowQueryKey(id ?? ""),
    enabled: id !== undefined,
    queryFn: () =>
      unwrap(client.GET("/v1/workflows/{workflow_id}", { params: { path: { workflow_id: id! } } })),
  });
}

/** Raw definition without the detail wrapper — the editor's graph source. */
export async function getWorkflow(id: string): Promise<WorkflowDefinition> {
  const detail = await unwrap(
    client.GET("/v1/workflows/{workflow_id}", { params: { path: { workflow_id: id } } }),
  );
  return detail.definition;
}

export function useCreateWorkflow() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: WorkflowUpsert) => unwrap(client.POST("/v1/workflows", { body })),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: workflowsQueryKey }),
  });
}

export function useUpdateWorkflow(id: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: WorkflowUpsert) =>
      unwrap(
        client.PATCH("/v1/workflows/{workflow_id}", {
          params: { path: { workflow_id: id } },
          body,
        }),
      ),
    onSuccess: (detail) => {
      void queryClient.invalidateQueries({ queryKey: workflowsQueryKey });
      queryClient.setQueryData(workflowQueryKey(id), detail);
    },
  });
}

export function useDeleteWorkflow() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (id: string) => {
      // Not unwrap(): 204 has no body — status + parsed error body.
      const { response, error } = await client.DELETE("/v1/workflows/{workflow_id}", {
        params: { path: { workflow_id: id } },
      });
      if (!response.ok) {
        throw apiErrorFromResponse(response, error);
      }
    },
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: workflowsQueryKey }),
  });
}