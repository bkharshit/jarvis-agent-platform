import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { Toaster } from "@/components/Toaster";
import { toast, useToastStore } from "./toast";

describe("toast store", () => {
  it("pushes and dismisses", () => {
    const { push, dismiss } = useToastStore.getState();
    act(() => {
      push("error", "save failed");
    });
    expect(useToastStore.getState().toasts).toHaveLength(1);
    expect(useToastStore.getState().toasts[0].message).toBe("save failed");
    act(() => {
      dismiss(useToastStore.getState().toasts[0].id);
    });
    expect(useToastStore.getState().toasts).toHaveLength(0);
  });
});

describe("<Toaster/>", () => {
  it("renders nothing when there are no toasts", () => {
    const { container } = render(<Toaster />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders pushed toasts and dismisses on click", async () => {
    const user = userEvent.setup();
    render(<Toaster />);
    act(() => {
      toast("error", "duplicate agent name");
    });
    expect(screen.getByText("duplicate agent name")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Dismiss notification" }));
    expect(screen.queryByText("duplicate agent name")).not.toBeInTheDocument();
  });
});