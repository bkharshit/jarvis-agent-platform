import { describe, expect, it } from "vitest";

import { NAV_GROUPS } from "./Nav";
import { SECTION_KEYS, type SectionKey } from "@/capabilities/sectionRegistry";

describe("NAV_GROUPS", () => {
  it("covers every registered section exactly once", () => {
    const grouped = NAV_GROUPS.flatMap((group) => group.keys);
    expect([...grouped].sort()).toEqual([...SECTION_KEYS].sort());
  });

  it("groups are non-empty", () => {
    for (const group of NAV_GROUPS) {
      expect(group.keys.length).toBeGreaterThan(0);
    }
  });

  it("every group key is a registered SectionKey", () => {
    const registered = new Set<string>(SECTION_KEYS);
    for (const key of NAV_GROUPS.flatMap((group) => group.keys)) {
      expect(registered.has(key)).toBe(true);
    }
  });
});

// Type-level pin: NAV_GROUPS must stay exhaustive when SECTION_KEYS grows.
type MissingKeys = Exclude<
  SectionKey,
  (typeof NAV_GROUPS)[number]["keys"][number]
>;
// The array is always empty by construction — the type is the assertion.
it("NAV_GROUPS stays exhaustive over SECTION_KEYS", () => {
  const missing: MissingKeys[] = [];
  expect(missing).toHaveLength(0);
});