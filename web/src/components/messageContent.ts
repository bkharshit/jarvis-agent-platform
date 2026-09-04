import type { components } from "@/api/schema";

type Message = components["schemas"]["Message"];

/**
 * Flatten a Message.content (string | text/image parts) to display text.
 * Shared by the execution transcript and the conversations browser so both
 * render the same payload the same way.
 */
export function contentToText(content: Message["content"]): string {
  if (typeof content === "string") return content;
  return content
    .map((part) => (part.type === "text" ? part.text : "[image]"))
    .filter(Boolean)
    .join("\n");
}