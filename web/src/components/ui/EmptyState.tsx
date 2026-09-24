import type { ReactNode } from "react";

export interface EmptyStateProps {
  icon?: ReactNode;
  title: string;
  description?: string;
  action?: ReactNode;
}

export function EmptyState({ icon, title, description, action }: EmptyStateProps) {
  return (
    <div className="flex flex-col items-center justify-center gap-3 py-16 text-center">
      {icon && (
        <div
          aria-hidden
          className="flex h-12 w-12 items-center justify-center rounded-full text-neutral-400"
          style={{
            background:
              "radial-gradient(circle, rgb(99 102 241 / 0.15), transparent 70%)",
          }}
        >
          {icon}
        </div>
      )}
      <h3 className="text-sm font-semibold text-neutral-200">{title}</h3>
      {description && <p className="max-w-sm text-xs text-neutral-500">{description}</p>}
      {action}
    </div>
  );
}