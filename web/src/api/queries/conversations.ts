import { useQuery } from "@tanstack/react-query";

import { client, unwrap } from "@/api/client";

/** Real per-(agent, session) transcript from the backend's conversations route. */
export function conversationQueryKey(agentId: string, sessionId: string) {
  return ["conversations", agentId, sessionId] as const;
}

export function useConversationMessages(agentId: string | undefined, sessionId: string | undefined) {
  return useQuery({
    queryKey: conversationQueryKey(agentId ?? "", sessionId ?? ""),
    enabled: agentId !== undefined && sessionId !== undefined,
    queryFn: () =>
      unwrap(
        client.GET("/v1/conversations/{agent_id}/{session_id}/messages", {
          params: { path: { agent_id: agentId!, session_id: sessionId! } },
        }),
      ),
  });
}