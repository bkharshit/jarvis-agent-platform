import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { renderWithProviders } from "@/test/render";

import { ConversationsList } from "./ConversationsList";

describe("<ConversationsList/>", () => {
  it("renders a session row derived from executions", async () => {
    renderWithProviders(<ConversationsList />, { initialEntries: ["/conversations"] });
    expect(await screen.findByText("session-7")).toBeInTheDocument();
    expect(screen.getByText("agent-1".slice(0, 12) + "…")).toBeInTheDocument();
  });

  it("links to the per-session transcript", async () => {
    renderWithProviders(<ConversationsList />, { initialEntries: ["/conversations"] });
    expect(await screen.findByText("session-7")).toHaveAttribute(
      "href",
      "/conversations/agent-1/session-7",
    );
  });
});