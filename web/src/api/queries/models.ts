import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { client, unwrap } from "@/api/client";

// GET /v1/models (ADR 0007): the live catalog a provider endpoint answers
// with — real data at the moment it answered, never a configured fact.

export interface ModelList {
  provider: string;
  base_url?: string | null;
  models: string[];
}

export const modelListQueryKey = (
  provider: string,
  baseUrl: string,
  apiKeyEnv: string,
) => ["models", provider, baseUrl, apiKeyEnv] as const;

/** Debounce a fast-changing value (base_url/api_key_env are typed by hand). */
function useDebounced<T>(value: T, delayMs: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(value), delayMs);
    return () => clearTimeout(timer);
  }, [value, delayMs]);
  return debounced;
}

export function useModelList(provider: string, baseUrl: string, apiKeyEnv: string) {
  const trimmedProvider = provider.trim();
  const debouncedBaseUrl = useDebounced(baseUrl.trim(), 500);
  const debouncedApiKeyEnv = useDebounced(apiKeyEnv.trim(), 500);
  return useQuery<ModelList>({
    queryKey: modelListQueryKey(
      trimmedProvider,
      debouncedBaseUrl,
      debouncedApiKeyEnv,
    ),
    enabled: trimmedProvider !== "",
    // The catalog is live endpoint state, but it doesn't change per keystroke
    // of the surrounding form — a short stale time keeps retypes cheap.
    staleTime: 60_000,
    retry: false,
    refetchOnWindowFocus: false,
    queryFn: () =>
      unwrap(
        client.GET("/v1/models", {
          params: {
            query: {
              provider: trimmedProvider,
              // Empty optionals are omitted, never nulled (D19: backend
              // fills absent values from environment defaults).
              ...(debouncedBaseUrl !== "" ? { base_url: debouncedBaseUrl } : {}),
              ...(debouncedApiKeyEnv !== "" ? { api_key_env: debouncedApiKeyEnv } : {}),
            },
          },
        }),
      ),
  });
}