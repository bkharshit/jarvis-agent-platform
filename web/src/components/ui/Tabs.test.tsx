import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { Tabs } from "./Tabs";

const ITEMS = [
  { key: "configure", label: "Configure" },
  { key: "versions", label: "Versions" },
];

describe("Tabs", () => {
  it("renders a tablist with aria-selected on the active tab", () => {
    render(<Tabs items={ITEMS} value="configure" onValueChange={() => {}} ariaLabel="Agent" />);

    expect(screen.getByRole("tablist", { name: "Agent" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Configure" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.getByRole("tab", { name: "Versions" })).toHaveAttribute(
      "aria-selected",
      "false",
    );
  });

  it("fires onValueChange with the clicked key", async () => {
    const onValueChange = vi.fn();
    const user = userEvent.setup();
    render(
      <Tabs items={ITEMS} value="configure" onValueChange={onValueChange} ariaLabel="Agent" />,
    );

    await user.click(screen.getByRole("tab", { name: "Versions" }));
    expect(onValueChange).toHaveBeenCalledWith("versions");
  });

  it("supports the segmented variant", () => {
    render(
      <Tabs
        items={ITEMS}
        value="configure"
        onValueChange={() => {}}
        variant="segmented"
        ariaLabel="View"
      />,
    );
    expect(screen.getByRole("tab", { name: "Configure" }).className).toContain("bg-neutral-800");
  });
});