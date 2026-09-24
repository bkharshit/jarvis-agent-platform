import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { Plus } from "lucide-react";

import { Button } from "./Button";

describe("Button", () => {
  it("renders its label and fires clicks", async () => {
    const onClick = vi.fn();
    const user = userEvent.setup();
    render(<Button onClick={onClick}>Save</Button>);

    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(onClick).toHaveBeenCalledOnce();
  });

  it("defaults to a type=button so it never submits a form implicitly", () => {
    render(<Button>Save</Button>);
    expect(screen.getByRole("button", { name: "Save" })).toHaveAttribute("type", "button");
  });

  it("applies variant classes", () => {
    render(<Button variant="danger">Delete</Button>);
    expect(screen.getByRole("button", { name: "Delete" }).className).toContain("rose");
  });

  it("loading disables the button and keeps the label", () => {
    render(<Button loading>Save</Button>);
    const button = screen.getByRole("button", { name: /save/i });
    expect(button).toBeDisabled();
    expect(button).toHaveTextContent("Save");
    expect(screen.getByRole("button", { name: /save/i })).toHaveAttribute("aria-busy", "true");
  });

  it("renders a leading icon", () => {
    render(<Button icon={<Plus aria-hidden />}>Add</Button>);
    expect(screen.getByRole("button", { name: /add/i })).toBeInTheDocument();
  });

  it("forwards refs so call-sites can focus after errors", () => {
    const ref = vi.fn();
    render(<Button ref={ref}>Save</Button>);
    expect(ref).toHaveBeenCalled();
  });

  it("explicit disabled={false} is not overridden by loading=false", () => {
    render(<Button disabled={false}>Save</Button>);
    expect(screen.getByRole("button", { name: "Save" })).toBeEnabled();
  });
});