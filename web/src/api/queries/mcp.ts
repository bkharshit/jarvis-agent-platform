import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { client, unwrap } from "@/api/client";
import { apiErrorFromResponse } from "@/api/errors";

import type { components } from "@/api/schema";

// MCP server management (S4, ADR 0012 §5): tenant-scoped rows, admin/owner
// writes (anonymous mode = full access), any-member list/probe. Types come
// from the generated schema; the 204 delete goes through the raw client.

export type McpServer = components["schemas"]["McpServer"];
export type McpServerCreate = components["schemas"]["McpServerCreate"];
export type McpServerPatch = components["schemas"]["McpServerPatch"];
export type McpProbeResponse = components["schemas"]["McpProbeResponse"];

// ADR 0013: header/env refs widen to the stored variant — the same
// credential-reference union the model credential_ref uses.
export type CredentialRef =
  | components["schemas"]["EnvCredentialRef"]
  | components["schemas"]["StoredCredentialRef"];

export const mcpServersQueryKey = ["mcp-servers"] as const;

export function useMcpServers() {
  return useQuery({
    queryKey: mcpServersQueryKey,
    queryFn: async () => {
      const body = await unwrap(client.GET("/v1/mcp/servers"));
      return body.items;
    },
  });
}

export function useCreateMcpServer() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: McpServerCreate) =>
      unwrap(client.POST("/v1/mcp/servers", { body })),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: mcpServersQueryKey }),
  });
}

export function useUpdateMcpServer() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ serverId, body }: { serverId: string; body: McpServerPatch }) =>
      unwrap(client.PATCH("/v1/mcp/servers/{server_id}", {
        params: { path: { server_id: serverId } },
        body,
      })),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: mcpServersQueryKey }),
  });
}

export function useDeleteMcpServer() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (serverId: string) => {
      const { response, error } = await client.DELETE("/v1/mcp/servers/{server_id}", {
        params: { path: { server_id: serverId } },
      });
      if (!response.ok) throw apiErrorFromResponse(response, error);
    },
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: mcpServersQueryKey }),
  });
}

// Probe is a POST (it connects fresh and persists nothing), but it is a
// read semantically — a query keyed by server id keeps the "View tools"
// interaction declarative; a failed connect surfaces as the 502 envelope.
export function useProbeMcpServer(serverId: string) {
  return useQuery({
    queryKey: ["mcp-probe", serverId],
    enabled: serverId !== "",
    queryFn: async () => {
      const body = await unwrap(client.POST("/v1/mcp/servers/{server_id}/probe", {
        params: { path: { server_id: serverId } },
      }));
      return body;
    },
  });
}