import { forwardRef, type TextareaHTMLAttributes } from "react";

import { cn } from "@/lib/utils";

const textareaClass =
  "w-full rounded-md border border-neutral-800 bg-neutral-900/70 px-3 py-2 text-sm text-neutral-100 transition-colors placeholder:text-neutral-500 focus:border-indigo-500 focus:outline-none focus:ring-2 focus:ring-indigo-500/30 disabled:cursor-not-allowed disabled:opacity-50";

export const Textarea = forwardRef<
  HTMLTextAreaElement,
  TextareaHTMLAttributes<HTMLTextAreaElement>
>(({ className, ...props }, ref) => (
  <textarea ref={ref} className={cn(textareaClass, className)} {...props} />
));
Textarea.displayName = "Textarea";