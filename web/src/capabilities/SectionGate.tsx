import type { ReactNode } from "react";

import { ComingSoon } from "@/capabilities/ComingSoon";
import type { SectionKey } from "@/capabilities/sectionRegistry";
import { useCapabilities } from "@/capabilities/useCapabilities";
import { Button } from "@/components/ui/Button";

// Route guard: every section route renders from the capabilities payload.
// Loading and error are honest full-screen states — the shell never renders
// as if everything were enabled.

function FullScreen({ children }: { children: ReactNode }) {
  return (
    <div className="flex min-h-screen items-center justify-center bg-[#0d0f12] text-neutral-400">
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
        <div className="rounded-xl border border-neutral-800/80 bg-neutral-900/50 p-8 text-center shadow-lg">
          <p className="text-neutral-200">Cannot reach the JARVIS backend.</p>
          <p className="mt-1 text-xs text-neutral-500">
            Start it with <code className="font-mono">uv run jarvis serve</code>, then retry.
          </p>
          <Button
            variant="secondary"
            className="mt-4"
            onClick={() => void refetch()}
          >
            Retry
          </Button>
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