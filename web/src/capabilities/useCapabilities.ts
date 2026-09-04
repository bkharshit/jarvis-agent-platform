import { useQuery } from "@tanstack/react-query";

import { fetchJson } from "@/api/http";

import type { Capabilities } from "./types";

// Capabilities are a fact of the running backend version — they change only
// on server restart, so the payload is fetched once and never refetched.
export const capabilitiesQueryKey = ["capabilities"] as const;

export function useCapabilities() {
  return useQuery<Capabilities>({
    queryKey: capabilitiesQueryKey,
    queryFn: () => fetchJson<Capabilities>("/v1/capabilities"),
    staleTime: Infinity,
    gcTime: Infinity,
    retry: 1,
    refetchOnWindowFocus: false,
  });
}