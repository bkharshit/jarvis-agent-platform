import type { ReactNode } from "react";

import { ComingSoon } from "@/capabilities/ComingSoon";
import type { SectionKey } from "@/capabilities/sectionRegistry";
import { useCapabilities } from "@/capabilities/useCapabilities";

// Route guard: every section route renders from the capabilities payload.
// Loading and error are honest full-screen states — the shell never renders
// as if everything were enabled.

function FullScreen({ children }: { children: ReactNode }) {
  return (
    <div className="flex min-h-screen items-center justify-center bg-neutral-950 text-neutral-400">
      {children}
    </div>
  );
}

export function SectionGate({
  sectionKey,
  children,
}: {
  sectionKey: SectionKey;
  children: ReactNode;
}) {
  const { data, isPending, isError, refetch } = useCapabilities();

  if (isPending) {
    return <FullScreen>Connecting to the JARVIS backend…</FullScreen>;
  }
  if (isError) {
    return (
      <FullScreen>
        <div className="text-center">
          <p className="text-neutral-200">Cannot reach the JARVIS backend.</p>
          <p className="mt-1 text-xs">
            Start it with <code className="font-mono">uv run jarvis serve</code>, then retry.
          </p>
          <button
            type="button"
            onClick={() => void refetch()}
            className="mt-4 rounded bg-neutral-800 px-3 py-1.5 text-sm text-neutral-200 hover:bg-neutral-700"
          >
            Retry
          </button>
        </div>
      </FullScreen>
    );
  }

  const capability = data.sections[sectionKey];
  // Forward-compat: a backend may know sections this frontend doesn't.
  if (!capability) {
    return (
      <FullScreen>
        Unknown section <code className="font-mono">{sectionKey}</code>.
      </FullScreen>
    );
  }
  if (!capability.enabled) {
    return <ComingSoon sectionKey={sectionKey} capability={capability} />;
  }
  return <>{children}</>;
}