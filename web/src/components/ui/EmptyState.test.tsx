import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Bot } from "lucide-react";

import { EmptyState } from "./EmptyState";

describe("EmptyState", () => {
  it("renders title, description and action", () => {
    render(
      <EmptyState
        icon={<Bot />}
        title="No agents yet"
        description="Create your first agent to get started."
        action={<button>New agent</button>}
      />,
    );
    expect(screen.getByText("No agents yet")).toBeInTheDocument();
    expect(screen.getByText(/create your first agent/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "New agent" })).toBeInTheDocument();
  });

  it("omits description and action cleanly", () => {
    render(<EmptyState title="Nothing here" />);
    expect(screen.getByText("Nothing here")).toBeInTheDocument();
  });
});