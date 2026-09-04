import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { renderWithProviders } from "@/test/render";

import { ConversationDetailPage } from "./ConversationDetail";

describe("<ConversationDetailPage/>", () => {
  it("renders the transcript from the conversations endpoint", async () => {
    renderWithProviders(<ConversationDetailPage />, {
      path: "/conversations/:agentId/:sessionId",
      initialEntries: ["/conversations/agent-1/session-7"],
    });
    expect(await screen.findByText("what is 2+2?")).toBeInTheDocument();
    expect(screen.getByText("It is 4.")).toBeInTheDocument();
    expect(screen.getByText("agent agent-1")).toBeInTheDocument();
  });

  it("renders an honest empty state on 404", async () => {
    renderWithProviders(<ConversationDetailPage />, {
      path: "/conversations/:agentId/:sessionId",
      initialEntries: ["/conversations/agent-1/unknown-session"],
    });
    expect(await screen.findByText(/No conversation exists/)).toBeInTheDocument();
    expect(screen.getByText("← All conversations")).toBeInTheDocument();
  });
});