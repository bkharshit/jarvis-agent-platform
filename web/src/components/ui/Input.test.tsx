import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { Input } from "./Input";
import { Textarea } from "./Textarea";

describe("Input", () => {
  it("passes native props through and accepts typed text", async () => {
    const onChange = vi.fn();
    const user = userEvent.setup();
    render(
      <Input aria-label="Name" placeholder="agent name" onChange={onChange} />,
    );

    await user.type(screen.getByLabelText("Name"), "bot");
    expect(screen.getByLabelText("Name")).toHaveValue("bot");
    expect(onChange).toHaveBeenCalled();
  });

  it("forwards refs", () => {
    const ref = vi.fn();
    render(<Input ref={ref} aria-label="Name" />);
    expect(ref).toHaveBeenCalled();
  });
});

describe("Textarea", () => {
  it("accepts multi-line input and rows", async () => {
    const user = userEvent.setup();
    render(<Textarea aria-label="Prompt" rows={8} />);

    const el = screen.getByLabelText("Prompt") as HTMLTextAreaElement;
    await user.type(el, "line one{Enter}line two");
    expect(el).toHaveValue("line one\nline two");
    expect(el).toHaveAttribute("rows", "8");
  });

  it("forwards refs", () => {
    const ref = vi.fn();
    render(<Textarea ref={ref} aria-label="Prompt" />);
    expect(ref).toHaveBeenCalled();
  });
});