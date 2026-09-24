import {
  Activity,
  Bot,
  BookOpen,
  ClipboardCheck,
  Cpu,
  LineChart,
  MessagesSquare,
  Puzzle,
  Settings,
  Wrench,
  Workflow,
  Zap,
  type LucideIcon,
} from "lucide-react";
import { NavLink } from "react-router";

import { Badge } from "@/components/ui/Badge";
import { cn } from "@/lib/utils";
import {
  SECTION_KEYS,
  SECTION_REGISTRY,
  type SectionKey,
} from "@/capabilities/sectionRegistry";
import { useCapabilities } from "@/capabilities/useCapabilities";

// Presentation-only grouping over the single source of truth (sectionRegistry).
// Every SectionKey appears exactly once across NAV_GROUPS — Nav.test pins it —
// so no section can silently disappear from the shell. Enablement is never
// decided here; disabled sections stay visible, dimmed, with their stage.

const SECTION_ICONS: Record<SectionKey, LucideIcon> = {
  agents: Bot,
  executions: Activity,
  conversations: MessagesSquare,
  tools: Wrench,
  models: Cpu,
  workflows: Workflow,
  knowledge: BookOpen,
  evaluations: ClipboardCheck,
  observability: LineChart,
  plugins: Puzzle,
  triggers: Zap,
  settings: Settings,
};

export interface NavGroup {
  label: string;
  keys: SectionKey[];
}

export const NAV_GROUPS: NavGroup[] = [
  { label: "Studio", keys: ["agents", "workflows"] },
  { label: "Knowledge", keys: ["knowledge"] },
  { label: "Tools", keys: ["tools", "plugins"] },
  { label: "Monitoring", keys: ["executions", "observability", "conversations"] },
  { label: "Build", keys: ["models", "evaluations", "triggers"] },
  { label: "Account", keys: ["settings"] },
];

export function NavGroupLinks({ group }: { group: NavGroup }) {
  const { data } = useCapabilities();

  return (
    <>
      {group.keys.map((key) => {
        const capability = data?.sections[key];
        const enabled = capability?.enabled ?? false;
        const Icon = SECTION_ICONS[key];
        return (
          <NavLink
            key={key}
            to={SECTION_REGISTRY[key].route}
            aria-label={SECTION_REGISTRY[key].title}
            className={({ isActive }) =>
              cn(
                "inline-flex items-center gap-1.5 rounded-md px-2.5 py-1.5 text-sm whitespace-nowrap transition-colors",
                isActive
                  ? "bg-neutral-800 text-neutral-100"
                  : "text-neutral-400 hover:bg-neutral-800/60 hover:text-neutral-200",
                !enabled && "text-neutral-500 hover:text-neutral-400",
              )
            }
          >
            <Icon aria-hidden className="h-4 w-4 shrink-0" />
            <span>{SECTION_REGISTRY[key].title}</span>
            {!enabled && (
              <Badge
                variant="neutral"
                title={`enabled by stage ${capability?.stage ?? "TBD"}`}
              >
                {capability?.stage ?? "TBD"}
              </Badge>
            )}
          </NavLink>
        );
      })}
    </>
  );
}

// Derived, honest status: how many IA sections the backend reports live.
export function useSectionStatus() {
  const { data } = useCapabilities();
  const enabledCount = SECTION_KEYS.filter(
    (key) => data?.sections[key]?.enabled ?? false,
  ).length;
  const variant: "success" | "warning" =
    enabledCount >= SECTION_KEYS.length - 2 ? "success" : "warning";
  return {
    enabledCount,
    total: SECTION_KEYS.length,
    variant,
  };
}