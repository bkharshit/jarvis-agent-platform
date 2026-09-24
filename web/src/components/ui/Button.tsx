import { forwardRef, type ButtonHTMLAttributes, type ReactNode } from "react";
import { Loader2 } from "lucide-react";
import { cva, type VariantProps } from "class-variance-authority";

import { cn } from "@/lib/utils";

const buttonVariants = cva(
  "inline-flex cursor-pointer items-center justify-center gap-1.5 rounded-md text-sm font-medium transition-colors outline-none focus-visible:ring-2 focus-visible:ring-indigo-500/60 disabled:cursor-not-allowed disabled:opacity-50",
  {
    variants: {
      variant: {
        primary: "bg-indigo-600 text-white shadow-sm hover:bg-indigo-500",
        secondary:
          "border border-neutral-700/60 bg-neutral-800 text-neutral-200 hover:bg-neutral-700",
        ghost: "text-neutral-400 hover:bg-neutral-800/60 hover:text-neutral-100",
        danger:
          "border border-rose-800/50 bg-rose-950/40 text-rose-300 hover:bg-rose-900/50",
      },
      size: {
        sm: "h-7 px-2.5 text-xs",
        md: "h-9 px-3.5",
        lg: "h-10 px-5 text-base",
        icon: "h-8 w-8 p-0",
      },
    },
    defaultVariants: {
      variant: "primary",
      size: "md",
    },
  },
);

export interface ButtonProps
  extends ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  loading?: boolean;
  icon?: ReactNode;
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(
  (
    { className, variant, size, loading = false, icon, children, disabled, type = "button", ...props },
    ref,
  ) => (
    <button
      ref={ref}
      type={type}
      className={cn(buttonVariants({ variant, size }), className)}
      disabled={disabled ?? loading}
      aria-busy={loading || undefined}
      {...props}
    >
      {loading ? <Loader2 aria-hidden className="h-4 w-4 animate-spin" /> : icon}
      {children}
    </button>
  ),
);
Button.displayName = "Button";