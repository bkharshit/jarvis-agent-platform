import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { client, unwrap } from "@/api/client";
import { apiErrorFromResponse } from "@/api/errors";

import type { components } from "@/api/schema";

// Settings surfaces (S2): tenant members, API keys, BYOK credentials. Every
// type comes from the generated schema; the delete/revoke routes return 204
// with no body, so they go through the raw client like useDeleteAgent.

export type MemberOut = components["schemas"]["MemberOut"];
export type MemberCreate = components["schemas"]["MemberCreate"];
export type MemberPatch = components["schemas"]["MemberPatch"];
export type ApiKeyOut = components["schemas"]["ApiKeyOut"];
export type ApiKeyCreated = components["schemas"]["ApiKeyCreated"];
export type CredentialOut = components["schemas"]["CredentialOut"];
export type CredentialCreate = components["schemas"]["CredentialCreate"];
export type CredentialPatch = components["schemas"]["CredentialPatch"];

// --- members -------------------------------------------------------------

export const membersQueryKey = ["members"] as const;

export function useMembers() {
  return useQuery({
    queryKey: membersQueryKey,
    queryFn: async () => {
      const body = await unwrap(client.GET("/v1/members"));
      return body.items;
    },
  });
}

export function useCreateMember() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: MemberCreate) => unwrap(client.POST("/v1/members", { body })),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: membersQueryKey }),
  });
}

export function useUpdateMember() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ userId, body }: { userId: string; body: MemberPatch }) =>
      unwrap(client.PATCH("/v1/members/{user_id}", {
        params: { path: { user_id: userId } },
        body,
      })),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: membersQueryKey }),
  });
}

export function useDeleteMember() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (userId: string) => {
      const { response, error } = await client.DELETE("/v1/members/{user_id}", {
        params: { path: { user_id: userId } },
      });
      if (!response.ok) throw apiErrorFromResponse(response, error);
    },
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: membersQueryKey }),
  });
}

// --- API keys ------------------------------------------------------------

export const apiKeysQueryKey = ["api-keys"] as const;

export function useApiKeys() {
  return useQuery({
    queryKey: apiKeysQueryKey,
    queryFn: async () => {
      const body = await unwrap(client.GET("/v1/api-keys"));
      return body.items;
    },
  });
}

// Create returns the plaintext exactly once; the caller holds it in page
// state — nothing re-fetches it, and the list payload never carries it.
export function useCreateApiKey() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: components["schemas"]["ApiKeyCreate"]) =>
      unwrap(client.POST("/v1/api-keys", { body })),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: apiKeysQueryKey }),
  });
}

export function useRevokeApiKey() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (keyId: string) => {
      const { response, error } = await client.DELETE("/v1/api-keys/{key_id}", {
        params: { path: { key_id: keyId } },
      });
      if (!response.ok) throw apiErrorFromResponse(response, error);
    },
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: apiKeysQueryKey }),
  });
}

// --- BYOK credentials ------------------------------------------------------

export const credentialsQueryKey = ["credentials"] as const;

export function useCredentials() {
  return useQuery({
    queryKey: credentialsQueryKey,
    queryFn: async () => {
      const body = await unwrap(client.GET("/v1/credentials"));
      return body.items;
    },
  });
}

export function useCreateCredential() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: CredentialCreate) =>
      unwrap(client.POST("/v1/credentials", { body })),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: credentialsQueryKey }),
  });
}

export function useUpdateCredential() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ credentialId, body }: { credentialId: string; body: CredentialPatch }) =>
      unwrap(client.PATCH("/v1/credentials/{credential_id}", {
        params: { path: { credential_id: credentialId } },
        body,
      })),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: credentialsQueryKey }),
  });
}

export function useRevokeCredential() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (credentialId: string) => {
      const { response, error } = await client.DELETE("/v1/credentials/{credential_id}", {
        params: { path: { credential_id: credentialId } },
      });
      if (!response.ok) throw apiErrorFromResponse(response, error);
    },
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: credentialsQueryKey }),
  });
}