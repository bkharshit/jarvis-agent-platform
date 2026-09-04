import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { client, unwrap } from "@/api/client";
import { apiErrorFromResponse } from "@/api/errors";

import type { components } from "@/api/schema";

// All types come from the generated schema — no hand-written shapes here.
export type AgentDefinition = components["schemas"]["AgentDefinition"];
export type AgentDetail = components["schemas"]["AgentDetail"];
export type AgentUpsert = components["schemas"]["AgentUpsertRequest"];
export type ToolBinding = components["schemas"]["ToolBinding"];

export const agentsQueryKey = ["agents"] as const;
export const agentQueryKey = (id: string) => ["agents", id] as const;

export function useAgents() {
  return useQuery({
    queryKey: agentsQueryKey,
    queryFn: async () => {
      const body = await unwrap(client.GET("/v1/agents"));
      return body.items;
    },
  });
}

export function useAgent(id: string | undefined) {
  return useQuery({
    queryKey: agentQueryKey(id ?? ""),
    enabled: id !== undefined,
    queryFn: () => unwrap(client.GET("/v1/agents/{agent_id}", { params: { path: { agent_id: id! } } })),
  });
}

export function useCreateAgent() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: AgentUpsert) =>
      unwrap(client.POST("/v1/agents", { body })),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: agentsQueryKey }),
  });
}

export function useUpdateAgent(id: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: AgentUpsert) =>
      unwrap(client.PATCH("/v1/agents/{agent_id}", {
        params: { path: { agent_id: id } },
        body,
      })),
    onSuccess: (detail) => {
      void queryClient.invalidateQueries({ queryKey: agentsQueryKey });
      queryClient.setQueryData(agentQueryKey(id), detail);
    },
  });
}

export function useDeleteAgent() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (id: string) => {
      // Not unwrap(): 204 has no body — use the response status, with the
      // client's already-parsed error body for the envelope.
      const { response, error } = await client.DELETE("/v1/agents/{agent_id}", {
        params: { path: { agent_id: id } },
      });
      if (!response.ok) {
        throw apiErrorFromResponse(response, error);
      }
    },
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: agentsQueryKey }),
  });
}