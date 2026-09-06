import { QueryCache, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router";

import App from "./App";
import { whoamiQueryKey } from "@/api/queries/auth";
import { ApiError } from "@/api/errors";
import { toast } from "@/stores/toast";
import "./index.css";

const queryClient = new QueryClient({
  queryCache: new QueryCache({
    onError: (error) => {
      // The 401 funnel (S2): one rejected request anywhere means the
      // session/key is gone. Flip the whoami fact — the Settings section
      // then renders its sign-in surface — and say so, instead of leaving
      // every panel guessing. No forced navigation mid-task.
      if (error instanceof ApiError && error.status === 401) {
        queryClient.setQueryData(whoamiQueryKey, null);
        toast("error", "Your session has ended — sign in again under Settings.");
      }
    },
  }),
  defaultOptions: {
    queries: {
      refetchOnWindowFocus: false,
    },
  },
});

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
);