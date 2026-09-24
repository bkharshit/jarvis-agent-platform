import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Field } from "./Field";
import { Input } from "./Input";

describe("Field", () => {
  it("wires the label to the input via htmlFor/id", () => {
    render(
      <Field label="Name" htmlFor="agent-name">
        <Input id="agent-name" />
      </Field>,
    );
    expect(screen.getByLabelText("Name")).toBe(screen.getByRole("textbox"));
  });

  it("shows the error with role=alert only when present", () => {
    const { rerender } = render(
      <Field label="Name" error="name is required">
        <Input aria-label="Name" />
      </Field>,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("name is required");

    rerender(
      <Field label="Name" error={null}>
        <Input aria-label="Name" />
      </Field>,
    );
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("shows the hint while there is no error", () => {
    const { rerender } = render(
      <Field label="Model" hint="type an id manually if the listing fails">
        <Input aria-label="Model" />
      </Field>,
    );
    expect(screen.getByText(/type an id manually/i)).toBeInTheDocument();

    rerender(
      <Field label="Model" hint="hint" error="bad model">
        <Input aria-label="Model" />
      </Field>,
    );
    expect(screen.queryByText("hint")).not.toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("bad model");
  });
});