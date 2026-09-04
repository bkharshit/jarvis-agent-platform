import type { SectionKey } from "@/capabilities/sectionRegistry";
import { SECTION_REGISTRY } from "@/capabilities/sectionRegistry";
import type { SectionCapability } from "@/capabilities/types";

interface ComingSoonProps {
  sectionKey: SectionKey;
  capability: SectionCapability;
}

// The one shared disabled-section panel: name, one-line summary, and the
// roadmap stage that enables it. No per-section bespoke stubs.
export function ComingSoon({ sectionKey, capability }: ComingSoonProps) {
  return (
    <div className="mx-auto max-w-2xl px-6 py-16 text-center">
      <h1 className="text-2xl font-semibold text-neutral-100">
        {SECTION_REGISTRY[sectionKey].title}
      </h1>
      <p className="mt-3 text-sm text-neutral-400">
        {capability.summary ?? "This section is not implemented yet."}
      </p>
      <p className="mt-6 inline-block rounded-full border border-neutral-700 bg-neutral-900 px-3 py-1 text-xs text-neutral-400">
        Coming soon — enabled by stage{" "}
        <span className="font-mono text-neutral-200">{capability.stage ?? "TBD"}</span>
      </p>
    </div>
  );
}