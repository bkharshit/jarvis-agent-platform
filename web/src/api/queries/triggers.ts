import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { client, unwrap } from "@/api/client";
import { apiErrorFromResponse } from "@/api/errors";

import type { components } from "@/api/schema";

// Trigger management (S13, ADR 0019 §6, D62): tenant-scoped rows, admin/
// owner writes (anonymous mode = full access), any-member list. The webhook
// secret's plaintext exists EXACTLY ONCE — the create and rotate responses —
// and is never in any GET; the UI treats it as display-once state.

export type TriggerOut = components["schemas"]["TriggerOut"];
export type TriggerCreate = components["schemas"]["TriggerCreate"];
export type TriggerPatch = components["schemas"]["TriggerPatch"];
export type TriggerDetail = components["schemas"]["TriggerDetail"];
export type TriggerCreated = components["schemas"]["TriggerCreated"];

export const triggersQueryKey = ["triggers"] as const;
export const triggerDetailQueryKey = (id: string) => ["triggers", id] as const;

export function useTriggers() {
  return useQuery({
    queryKey: triggersQueryKey,
    queryFn: async () => {
      const body = await unwrap(client.GET("/v1/triggers"));
      return body.items;
    },
  });
}

export function useTrigger(triggerId: string) {
  return useQuery({
    queryKey: triggerDetailQueryKey(triggerId),
    enabled: triggerId !== "",
    queryFn: async () => {
      const body = await unwrap(
        client.GET("/v1/triggers/{trigger_id}", {
          params: { path: { trigger_id: triggerId } },
        }),
      );
      return body;
    },
  });
}

export function useCreateTrigger() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: TriggerCreate) =>
      unwrap(client.POST("/v1/triggers", { body })),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: triggersQueryKey }),
  });
}

// Full-document PATCH (the mcp/eval discipline): the body is the whole
// trigger document, never a diff — the editor always sends every field.
export function useUpdateTrigger() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ triggerId, body }: { triggerId: string; body: TriggerPatch }) =>
      unwrap(
        client.PATCH("/v1/triggers/{trigger_id}", {
          params: { path: { trigger_id: triggerId } },
          body,
        }),
      ),
    onSuccess: (_data, vars) => {
      void queryClient.invalidateQueries({ queryKey: triggersQueryKey });
      void queryClient.invalidateQueries({ queryKey: triggerDetailQueryKey(vars.triggerId) });
    },
  });
}

export function useDeleteTrigger() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (triggerId: string) => {
      const { response, error } = await client.DELETE("/v1/triggers/{trigger_id}", {
        params: { path: { trigger_id: triggerId } },
      });
      if (!response.ok) throw apiErrorFromResponse(response, error);
    },
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: triggersQueryKey }),
  });
}

// Rotate returns the new plaintext EXACTLY ONCE (D29/D30) — the mutation's
// result is display-once state; it is never cached into the query store.
export function useRotateTriggerSecret() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (triggerId: string) =>
      unwrap(
        client.POST("/v1/triggers/{trigger_id}/rotate-secret", {
          params: { path: { trigger_id: triggerId } },
        }),
      ),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: triggersQueryKey }),
  });
}

export function useFireTrigger() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (triggerId: string) =>
      unwrap(
        client.POST("/v1/triggers/{trigger_id}/fire", {
          params: { path: { trigger_id: triggerId } },
        }),
      ),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["executions"] }),
  });
}

/** The runs a trigger fired — the executions listing filtered by the
 * provenance column (D62); the trigger row may itself be deleted. */
export function executionsQueryKeyWithTrigger(triggerId: string) {
  return ["executions", { agent: null, status: null, trigger: triggerId }] as const;
}

export function useTriggerRuns(triggerId: string) {
  return useQuery({
    queryKey: executionsQueryKeyWithTrigger(triggerId),
    enabled: triggerId !== "",
    queryFn: async () => {
      const body = await unwrap(
        client.GET("/v1/executions", {
          params: { query: { trigger_id: triggerId } },
        }),
      );
      return { items: body.items, names: body.names ?? {} };
    },
  });
}