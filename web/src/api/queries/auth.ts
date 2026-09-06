import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { client, unwrap } from "@/api/client";
import { ApiError } from "@/api/errors";

import type { components } from "@/api/schema";

// Auth state (S2). whoami is the single fact the shell's signed-in-ness
// hangs on: null (401) is a *state* — unauthenticated — not a query error,
// so pages branch on it instead of catching.

export type Whoami = components["schemas"]["WhoamiResponse"];

export const whoamiQueryKey = ["whoami"] as const;

export function useWhoami() {
  return useQuery<Whoami | null>({
    queryKey: whoamiQueryKey,
    queryFn: async () => {
      try {
        return await unwrap(client.GET("/v1/auth/whoami"));
      } catch (error) {
        if (error instanceof ApiError && error.status === 401) return null;
        throw error;
      }
    },
    retry: false,
    refetchOnWindowFocus: false,
  });
}

export function useLogin() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: components["schemas"]["LoginRequest"]) =>
      unwrap(client.POST("/v1/auth/login", { body })),
    // The cookie is set server-side; the whoami fact flips to it.
    onSuccess: (whoami) => queryClient.setQueryData(whoamiQueryKey, whoami),
  });
}

export function useLogout() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async () => {
      // Not unwrap(): 204 has no body.
      const { response } = await client.POST("/v1/auth/logout");
      if (!response.ok) throw new ApiError(response.status, "internal", "logout failed");
      return;
    },
    onSettled: () => queryClient.setQueryData(whoamiQueryKey, null),
  });
}