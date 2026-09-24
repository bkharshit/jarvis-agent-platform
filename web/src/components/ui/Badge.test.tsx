import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Badge } from "./Badge";

describe("Badge", () => {
  it("renders children", () => {
    render(<Badge>running</Badge>);
    expect(screen.getByText("running")).toBeInTheDocument();
  });

  it("applies variant classes", () => {
    render(<Badge variant="success">ok</Badge>);
    expect(screen.getByText("ok").className).toContain("emerald");
  });

  it("renders a status dot only when dot is set", () => {
    const { rerender } = render(<Badge dot>live</Badge>);
    expect(screen.getByText("live").querySelector("span")).not.toBeNull();

    rerender(<Badge>live</Badge>);
    expect(screen.getByText("live").querySelector("span")).toBeNull();
  });

  it("passes title through (nav stage badge depends on it)", () => {
    render(<Badge title="enabled by stage S6">S6</Badge>);
    expect(screen.getByTitle("enabled by stage S6")).toHaveTextContent("S6");
  });
});