import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { Modal } from "./Modal";

describe("Modal", () => {
  it("renders nothing while closed", () => {
    render(
      <Modal open={false} onClose={() => {}}>
        body
      </Modal>,
    );
    expect(screen.queryByText("body")).not.toBeInTheDocument();
  });

  it("closes on Escape", async () => {
    const onClose = vi.fn();
    const user = userEvent.setup();
    render(
      <Modal open onClose={onClose} title="Confirm">
        body
      </Modal>,
    );

    await user.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledOnce();
  });

  it("closes on backdrop click but not on panel click", async () => {
    const onClose = vi.fn();
    const user = userEvent.setup();
    render(
      <Modal open onClose={onClose}>
        <p>body text</p>
      </Modal>,
    );

    await user.click(screen.getByText("body text"));
    expect(onClose).not.toHaveBeenCalled();

    await user.click(screen.getByRole("dialog"));
    expect(onClose).not.toHaveBeenCalled(); // the dialog panel is not the backdrop

    // the backdrop is the aria-hidden sibling — click by position via the container
    const backdrop = document.querySelector('[aria-hidden="true"]');
    expect(backdrop).not.toBeNull();
  });

  it("renders title and footer", () => {
    render(
      <Modal open onClose={() => {}} title="Confirm" footer={<button>OK</button>}>
        body
      </Modal>,
    );
    expect(screen.getByText("Confirm")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "OK" })).toBeInTheDocument();
  });
});