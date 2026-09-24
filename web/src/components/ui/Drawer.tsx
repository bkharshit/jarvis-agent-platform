import { useEffect, type ReactNode } from "react";

import { cn } from "@/lib/utils";

export interface DrawerProps {
  open: boolean;
  onClose: () => void;
  title?: ReactNode;
  side?: "left" | "right";
  widthClassName?: string;
  className?: string;
  /** A non-modal drawer slides in with no backdrop — the page behind stays
   *  interactive (an editor side panel, not a dialog). Escape still closes. */
  modal?: boolean;
  children: ReactNode;
}

export function Drawer({
  open,
  onClose,
  title,
  side = "right",
  widthClassName = "w-[380px]",
  className,
  modal = true,
  children,
}: DrawerProps) {
  useEffect(() => {
    if (!open) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50">
      {modal && (
        <div
          aria-hidden
          onClick={onClose}
          className="absolute inset-0 animate-overlay-in bg-black/60 backdrop-blur-xs"
        />
      )}
      <aside
        role="dialog"
        aria-modal={modal ? "true" : undefined}
        aria-label={typeof title === "string" ? title : "Panel"}
        className={cn(
          "absolute top-0 bottom-0 flex flex-col border-neutral-800 bg-neutral-900/95 shadow-2xl backdrop-blur",
          side === "right"
            ? "right-0 animate-panel-in-right border-l"
            : "left-0 animate-panel-in-left border-r",
          widthClassName,
        )}
      >
        {title && (
          <div className="flex items-center justify-between border-b border-neutral-800 px-4 py-3">
            <h2 className="text-sm font-semibold text-neutral-100">{title}</h2>
          </div>
        )}
        <div className={cn("min-h-0 flex-1 overflow-y-auto p-4", className)}>{children}</div>
      </aside>
    </div>
  );
}