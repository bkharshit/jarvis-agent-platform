import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";

import { Breadcrumbs } from "./Breadcrumbs";

describe("Breadcrumbs", () => {
  it("renders links for non-final items and plain text for the last", () => {
    render(
      <MemoryRouter>
        <Breadcrumbs
          items={[
            { label: "Agents", to: "/agents" },
            { label: "Research Agent", to: "/agents/abc" },
            { label: "v1" },
          ]}
        />
      </MemoryRouter>,
    );

    const nav = screen.getByRole("navigation", { name: "Breadcrumb" });
    expect(nav).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Agents" })).toHaveAttribute("href", "/agents");
    expect(screen.getByText("v1").closest("a")).toBeNull();
  });
});