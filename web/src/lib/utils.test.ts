import { describe, expect, it } from "vitest";

import { cn } from "./utils";

describe("cn", () => {
  it("joins plain class names", () => {
    expect(cn("px-2", "py-1")).toBe("px-2 py-1");
  });

  it("lets a later Tailwind class win the same conflict group", () => {
    expect(cn("px-2", "px-4")).toBe("px-4");
    expect(cn("bg-neutral-950", "bg-[#0d0f12]")).toBe("bg-[#0d0f12]");
  });

  it("skips conditional falsy inputs", () => {
    const hide = false;
    expect(cn("px-2", hide && "py-1", undefined, null, "text-sm")).toBe(
      "px-2 text-sm",
    );
  });

  it("resolves clsx object syntax", () => {
    expect(cn("rounded", { "border-0": true, "ring-1": false })).toBe(
      "rounded border-0",
    );
  });
});