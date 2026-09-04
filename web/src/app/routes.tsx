import type { ReactNode } from "react";

import { AgentEditor } from "@/sections/agents/AgentEditor";
import { AgentDetailPage } from "@/sections/agents/AgentDetailPage";
import { AgentsList } from "@/sections/agents/AgentsList";
import { RunConsole } from "@/sections/agents/RunConsole";
import { ExecutionsList } from "@/sections/executions/ExecutionsList";
import { ExecutionDetailPage } from "@/sections/executions/ExecutionDetail";
import { ConversationsList } from "@/sections/conversations/ConversationsList";
import { ConversationDetailPage } from "@/sections/conversations/ConversationDetail";
import { ToolsPage } from "@/sections/tools/ToolsPage";
import { ModelsPage } from "@/sections/models/ModelsPage";

import { SectionGate } from "@/capabilities/SectionGate";
import type { SectionKey } from "@/capabilities/sectionRegistry";

// Transitional: enabled sections whose screens land in later commits of
// this build render this panel — no mocked data, no inert controls.
function SectionPlaceholder({ sectionKey }: { sectionKey: SectionKey }) {
  return (
    <div className="mx-auto max-w-2xl px-6 py-16 text-center text-neutral-400">
      <h1 className="text-2xl font-semibold text-neutral-100">
        {sectionKey}
      </h1>
      <p className="mt-3 text-sm">
        Enabled by the backend — its UI lands in the next commits of this build.
      </p>
    </div>
  );
}

function gated(sectionKey: SectionKey, element?: ReactNode) {
  return (
    <SectionGate sectionKey={sectionKey}>
      {element ?? <SectionPlaceholder sectionKey={sectionKey} />}
    </SectionGate>
  );
}

// Route table — one route per IA section; nested detail routes attach under
// their section as the screens land (agents/:id, executions/:id, …).
export function sectionRoutes() {
  return [
    { path: "/agents", element: gated("agents", <AgentsList />) },
    { path: "/agents/new", element: gated("agents", <AgentEditor />) },
    { path: "/agents/:agentId", element: gated("agents", <AgentDetailPage />) },
    { path: "/agents/:agentId/run", element: gated("agents", <RunConsole />) },
    { path: "/agents/:agentId/edit", element: gated("agents", <AgentEditor />) },
    { path: "/executions", element: gated("executions", <ExecutionsList />) },
    { path: "/executions/:runId", element: gated("executions", <ExecutionDetailPage />) },
    { path: "/conversations", element: gated("conversations", <ConversationsList />) },
    {
      path: "/conversations/:agentId/:sessionId",
      element: gated("conversations", <ConversationDetailPage />),
    },
    { path: "/tools", element: gated("tools", <ToolsPage />) },
    { path: "/models", element: gated("models", <ModelsPage />) },
    { path: "/workflows", element: gated("workflows") },
    { path: "/knowledge", element: gated("knowledge") },
    { path: "/evaluations", element: gated("evaluations") },
    { path: "/observability", element: gated("observability") },
    { path: "/plugins", element: gated("plugins") },
    { path: "/triggers", element: gated("triggers") },
    { path: "/settings", element: gated("settings") },
  ];
}