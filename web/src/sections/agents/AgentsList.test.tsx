import { http, HttpResponse } from "msw";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { envelope } from "@/test/handlers";
import { server } from "@/test/msw";
import { renderWithProviders } from "@/test/render";

import { AgentsList } from "./AgentsList";

describe("<AgentsList/>", () => {
  it("renders agents from the API payload", async () => {
    renderWithProviders(<AgentsList />, { initialEntries: ["/agents"] });
    expect(await screen.findByRole("link", { name: "Research agent" })).toBeInTheDocument();
    expect(screen.getByText("mock/mock-agent")).toBeInTheDocument();
    expect(screen.getByText("function_calling")).toBeInTheDocument();
  });

  it("shows an honest empty state", async () => {
    server.use(http.get("/v1/agents", () => HttpResponse.json({ items: [] })));
    renderWithProviders(<AgentsList />, { initialEntries: ["/agents"] });
    expect(await screen.findByText(/No agents yet/)).toBeInTheDocument();
  });

  it("shows the API error message on failure", async () => {
    server.use(
      http.get("/v1/agents", () =>
        HttpResponse.json(envelope("internal", "database unavailable"), { status: 500 }),
      ),
    );
    renderWithProviders(<AgentsList />, { initialEntries: ["/agents"] });
    expect(await screen.findByText("database unavailable")).toBeInTheDocument();
  });

  it("deletes an agent after confirmation", async () => {
    const user = userEvent.setup();
    const deleteSpy = vi.fn(() => new HttpResponse(null, { status: 204 }));
    server.use(http.delete("/v1/agents/:agent_id", () => deleteSpy()));
    vi.spyOn(window, "confirm").mockReturnValue(true);

    renderWithProviders(<AgentsList />, { initialEntries: ["/agents"] });
    await user.click(await screen.findByRole("button", { name: "Delete" }));
    await waitFor(() => expect(deleteSpy).toHaveBeenCalledTimes(1));
  });

  it("toasts the server message verbatim when delete conflicts (409)", async () => {
    const user = userEvent.setup();
    server.use(
      http.delete("/v1/agents/:agent_id", () =>
        HttpResponse.json(
          envelope("conflict", "agent has executions; cancel or purge them first"),
          { status: 409 },
        ),
      ),
    );
    vi.spyOn(window, "confirm").mockReturnValue(true);

    renderWithProviders(<AgentsList />, { initialEntries: ["/agents"] });
    await user.click(await screen.findByRole("button", { name: "Delete" }));
    expect(
      await screen.findByText("agent has executions; cancel or purge them first"),
    ).toBeInTheDocument();
  });
});