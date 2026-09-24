import { useEffect, useRef, useState, type ReactNode } from "react";
import { NavLink, Outlet } from "react-router";
import { LogOut } from "lucide-react";

import { useLogout, useWhoami } from "@/api/queries/auth";
import { LoginScreen } from "@/auth/LoginScreen";
import { Toaster } from "@/components/Toaster";
import { Badge } from "@/components/ui/Badge";
import { NAV_GROUPS, NavGroupLinks, useSectionStatus } from "@/app/Nav";

// The shell's 401 funnel: auth_mode=required and nobody is acting → the
// sign-in surface replaces the whole shell. Hangs on whoami alone — the
// capabilities payload itself 401s unsigned-in, so it can never gate this.
// (S2 shipped the funnel on Settings only; every other section showed raw
// 401 errors instead.)
function AuthGate({ children }: { children: ReactNode }) {
  const { data: whoami, isPending, isError } = useWhoami();
  // null is a *state* (401), not a query error — but a query error (backend
  // down) must not masquerade as "needs sign-in".
  if (!isPending && !isError && whoami === null) {
    return <LoginScreen />;
  }
  return <>{children}</>;
}

function CapabilityPill() {
  const { enabledCount, total, variant } = useSectionStatus();
  return (
    <Badge variant={variant} dot title={`${enabledCount} of ${total} sections live`}>
      {enabledCount}/{total} live
    </Badge>
  );
}

// The signed-in identity is the closest thing this deployment has to a
// "workspace" label; anonymous mode simply omits it — no invented tenant.
function AccountMenu() {
  const { data: whoami } = useWhoami();
  const logout = useLogout();
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    const onPointerDown = (event: PointerEvent) => {
      if (rootRef.current && !rootRef.current.contains(event.target as Node)) {
        setOpen(false);
      }
    };
    window.addEventListener("keydown", onKeyDown);
    window.addEventListener("pointerdown", onPointerDown);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("pointerdown", onPointerDown);
    };
  }, [open]);

  const email = whoami?.email ?? null;
  if (!email) return null;

  return (
    <div ref={rootRef} className="relative">
      <button
        type="button"
        aria-label="Account menu"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
        className="flex h-8 w-8 cursor-pointer items-center justify-center rounded-full border border-neutral-700 bg-neutral-800 text-xs font-semibold text-neutral-200 transition-colors hover:bg-neutral-700"
      >
        {email.charAt(0).toUpperCase()}
      </button>
      {open && (
        <div
          role="menu"
          className="absolute right-0 top-10 z-50 w-56 animate-panel-in-scale rounded-xl border border-neutral-800 bg-neutral-900 p-2 shadow-2xl"
        >
          <p className="truncate px-2 py-1.5 text-xs text-neutral-400">{email}</p>
          <button
            type="button"
            role="menuitem"
            onClick={() => logout.mutate()}
            className="flex w-full cursor-pointer items-center gap-2 rounded-md px-2 py-1.5 text-sm text-neutral-300 transition-colors hover:bg-neutral-800"
          >
            <LogOut aria-hidden className="h-4 w-4" />
            Sign out
          </button>
        </div>
      )}
    </div>
  );
}

export function Layout() {
  const settingsGroup = NAV_GROUPS[NAV_GROUPS.length - 1];
  const navGroups = NAV_GROUPS.slice(0, -1);

  return (
    <AuthGate>
      <div className="flex min-h-screen flex-col bg-[#0d0f12] text-neutral-100">
        <header className="sticky top-0 z-40 flex h-14 shrink-0 items-center gap-4 border-b border-neutral-800 bg-[#0d0f12]/90 px-4 backdrop-blur-md">
          <NavLink to="/agents" className="flex shrink-0 items-center gap-2">
            <span className="flex h-7 w-7 items-center justify-center rounded-lg bg-gradient-to-br from-indigo-400 to-violet-500 text-xs font-bold text-white">
              J
            </span>
            <span className="bg-gradient-to-r from-indigo-400 to-violet-400 bg-clip-text text-base font-semibold tracking-tight text-transparent">
              JARVIS
            </span>
          </NavLink>
          <nav
            aria-label="Sections"
            className="flex min-w-0 flex-1 items-center gap-1 overflow-x-auto"
          >
            {navGroups.map((group, index) => (
              <div key={group.label} className="flex items-center gap-1">
                {index > 0 && (
                  <span
                    aria-hidden
                    className="mx-1 h-5 w-px shrink-0 bg-neutral-800"
                  />
                )}
                <NavGroupLinks group={group} />
              </div>
            ))}
          </nav>
          <div className="flex shrink-0 items-center gap-3">
            <CapabilityPill />
            <NavGroupLinks group={settingsGroup} />
            <AccountMenu />
          </div>
        </header>
        <main className="min-w-0 flex-1">
          <Outlet />
        </main>
        <Toaster />
      </div>
    </AuthGate>
  );
}