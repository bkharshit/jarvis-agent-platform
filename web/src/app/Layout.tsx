import { NavLink, Outlet } from "react-router";

import { Toaster } from "@/components/Toaster";
import { SECTION_KEYS, SECTION_REGISTRY } from "@/capabilities/sectionRegistry";
import { useCapabilities } from "@/capabilities/useCapabilities";

// The nav renders every IA section — disabled ones are visible but marked
// coming-soon, so the product's shape is honest from day one.

function Nav() {
  const { data } = useCapabilities();

  return (
    <nav className="flex flex-col gap-0.5 p-2" aria-label="Sections">
      {SECTION_KEYS.map((key) => {
        const capability = data?.sections[key];
        const enabled = capability?.enabled ?? false;
        return (
          <NavLink
            key={key}
            to={SECTION_REGISTRY[key].route}
            className={({ isActive }) =>
              `flex items-center justify-between rounded px-3 py-1.5 text-sm ${
                isActive
                  ? "bg-neutral-800 text-neutral-100"
                  : "text-neutral-400 hover:bg-neutral-900 hover:text-neutral-200"
              }`
            }
          >
            <span className={enabled ? "" : "text-neutral-500"}>
              {SECTION_REGISTRY[key].title}
            </span>
            {!enabled && (
              <span
                className="rounded-full bg-neutral-800 px-1.5 py-0.5 text-[10px] text-neutral-500"
                title={`enabled by stage ${capability?.stage ?? "TBD"}`}
              >
                {capability?.stage ?? "TBD"}
              </span>
            )}
          </NavLink>
        );
      })}
    </nav>
  );
}

export function Layout() {
  return (
    <div className="min-h-screen bg-neutral-950 text-neutral-100">
      <div className="flex">
        <aside className="w-52 shrink-0 border-r border-neutral-800">
          <div className="px-4 py-4">
            <span className="text-lg font-semibold tracking-tight">JARVIS</span>
          </div>
          <Nav />
        </aside>
        <main className="min-w-0 flex-1">
          <Outlet />
        </main>
      </div>
      <Toaster />
    </div>
  );
}