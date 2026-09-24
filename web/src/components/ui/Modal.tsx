import { useEffect, type ReactNode } from "react";

import { cn } from "@/lib/utils";

const widthClass = {
  sm: "max-w-sm",
  md: "max-w-lg",
  lg: "max-w-2xl",
} as const;

export interface ModalProps {
  open: boolean;
  onClose: () => void;
  title?: ReactNode;
  children: ReactNode;
  footer?: ReactNode;
  width?: keyof typeof widthClass;
}

export function Modal({ open, onClose, title, children, footer, width = "md" }: ModalProps) {
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
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <div
        aria-hidden
        onClick={onClose}
        className="absolute inset-0 animate-overlay-in bg-black/60 backdrop-blur-xs"
      />
      <div
        role="dialog"
        aria-modal="true"
        aria-label={typeof title === "string" ? title : "Dialog"}
        className={cn(
          "relative w-full animate-panel-in-scale rounded-xl border border-neutral-800 bg-neutral-900 shadow-2xl",
          widthClass[width],
        )}
      >
        {title && (
          <div className="border-b border-neutral-800 px-4 py-3 text-sm font-semibold text-neutral-100">
            {title}
          </div>
        )}
        <div className="p-4">{children}</div>
        {footer && (
          <div className="flex justify-end gap-2 border-t border-neutral-800 px-4 py-3">
            {footer}
          </div>
        )}
      </div>
    </div>
  );
}