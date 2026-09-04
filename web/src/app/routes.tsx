import type { ReactNode } from "react";

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
    { path: "/agents", element: gated("agents") },
    { path: "/executions", element: gated("executions") },
    { path: "/conversations", element: gated("conversations") },
    { path: "/tools", element: gated("tools") },
    { path: "/models", element: gated("models") },
    { path: "/workflows", element: gated("workflows") },
    { path: "/knowledge", element: gated("knowledge") },
    { path: "/evaluations", element: gated("evaluations") },
    { path: "/observability", element: gated("observability") },
    { path: "/plugins", element: gated("plugins") },
    { path: "/triggers", element: gated("triggers") },
    { path: "/settings", element: gated("settings") },
  ];
}