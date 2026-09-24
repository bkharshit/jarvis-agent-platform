import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { Drawer } from "./Drawer";

describe("Drawer", () => {
  it("renders nothing while closed", () => {
    render(
      <Drawer open={false} onClose={() => {}}>
        body
      </Drawer>,
    );
    expect(screen.queryByText("body")).not.toBeInTheDocument();
  });

  it("closes on Escape", async () => {
    const onClose = vi.fn();
    const user = userEvent.setup();
    render(
      <Drawer open onClose={onClose} title="Node config">
        body
      </Drawer>,
    );

    await user.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledOnce();
  });

  it("closes on backdrop click", async () => {
    const onClose = vi.fn();
    const user = userEvent.setup();
    const { container } = render(
      <Drawer open onClose={onClose}>
        body
      </Drawer>,
    );

    await user.click(container.querySelector('[aria-hidden="true"]')!);
    expect(onClose).toHaveBeenCalledOnce();
  });

  it("supports both sides with a dialog role", () => {
    const { rerender } = render(
      <Drawer open onClose={() => {}} side="right">
        body
      </Drawer>,
    );
    expect(screen.getByRole("dialog")).toBeInTheDocument();

    rerender(
      <Drawer open onClose={() => {}} side="left">
        body
      </Drawer>,
    );
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });
});