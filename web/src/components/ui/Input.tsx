import { forwardRef, type InputHTMLAttributes } from "react";

import { cn } from "@/lib/utils";

const inputClass =
  "w-full rounded-md border border-neutral-800 bg-neutral-900/70 px-3 py-1.5 text-sm text-neutral-100 transition-colors placeholder:text-neutral-500 focus:border-indigo-500 focus:outline-none focus:ring-2 focus:ring-indigo-500/30 disabled:cursor-not-allowed disabled:opacity-50";

export const Input = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement>>(
  ({ className, ...props }, ref) => (
    <input ref={ref} className={cn(inputClass, className)} {...props} />
  ),
);
Input.displayName = "Input";