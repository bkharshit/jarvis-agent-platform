import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

export interface TabItem {
  key: string;
  label: string;
  icon?: ReactNode;
}

export interface TabsProps {
  items: TabItem[];
  value: string;
  onValueChange: (key: string) => void;
  variant?: "line" | "segmented";
  ariaLabel: string;
  className?: string;
}

export function Tabs({
  items,
  value,
  onValueChange,
  variant = "line",
  ariaLabel,
  className,
}: TabsProps) {
  return (
    <div
      role="tablist"
      aria-label={ariaLabel}
      className={cn(
        variant === "line"
          ? "flex items-center gap-4 border-b border-neutral-800"
          : "inline-flex items-center gap-0.5 rounded-lg bg-neutral-900 p-0.5",
        className,
      )}
    >
      {items.map((item) => {
        const active = item.key === value;
        return (
          <button
            key={item.key}
            type="button"
            role="tab"
            aria-selected={active}
            onClick={() => onValueChange(item.key)}
            className={cn(
              "inline-flex cursor-pointer items-center gap-1.5 text-sm font-medium transition-colors outline-none focus-visible:ring-2 focus-visible:ring-indigo-500/60",
              variant === "line"
                ? cn(
                    "-mb-px border-b-2 px-1 py-2",
                    active
                      ? "border-indigo-500 text-neutral-100"
                      : "border-transparent text-neutral-400 hover:text-neutral-200",
                  )
                : cn(
                    "rounded-md px-3 py-1",
                    active
                      ? "bg-neutral-800 text-neutral-100"
                      : "text-neutral-400 hover:text-neutral-200",
                  ),
            )}
          >
            {item.icon}
            {item.label}
          </button>
        );
      })}
    </div>
  );
}