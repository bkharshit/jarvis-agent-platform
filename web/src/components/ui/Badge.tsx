import type { HTMLAttributes, ReactNode } from "react";
import { cva, type VariantProps } from "class-variance-authority";

import { cn } from "@/lib/utils";

const badgeVariants = cva(
  "inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-medium",
  {
    variants: {
      variant: {
        success: "border border-emerald-500/20 bg-emerald-500/10 text-emerald-400",
        warning: "border border-amber-500/20 bg-amber-500/10 text-amber-400",
        danger: "border border-rose-500/20 bg-rose-500/10 text-rose-400",
        brand: "border border-indigo-500/20 bg-indigo-500/10 text-indigo-300",
        neutral: "border border-neutral-700/60 bg-neutral-800 text-neutral-400",
      },
    },
    defaultVariants: {
      variant: "neutral",
    },
  },
);

const dotClass: Record<NonNullable<VariantProps<typeof badgeVariants>["variant"]>, string> = {
  success: "bg-emerald-400",
  warning: "bg-amber-400",
  danger: "bg-rose-400",
  brand: "bg-indigo-400",
  neutral: "bg-neutral-500",
};

export interface BadgeProps
  extends HTMLAttributes<HTMLSpanElement>,
    VariantProps<typeof badgeVariants> {
  dot?: boolean;
  children: ReactNode;
}

export function Badge({ variant, dot = false, className, children, ...props }: BadgeProps) {
  return (
    <span className={cn(badgeVariants({ variant }), className)} {...props}>
      {dot && (
        <span
          aria-hidden
          className={cn("h-1.5 w-1.5 rounded-full", dotClass[variant ?? "neutral"])}
        />
      )}
      {children}
    </span>
  );
}