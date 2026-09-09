import { http, HttpResponse } from "msw";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import App from "@/App";
import { SectionGate } from "@/capabilities/SectionGate";
import { renderWithProviders, TEST_CAPABILITIES } from "@/test/render";
import { whoamiFixture } from "@/test/handlers";
import { server } from "@/test/msw";

describe("App shell", () => {
  it("redirects / to /agents and renders the nav from the payload", async () => {
    renderWithProviders(<App />, { initialEntries: ["/"] });

    await screen.findByRole("heading", { name: /agents/i });
    // enabled sections render as live nav links
    expect(screen.getByRole("link", { name: /Executions/ })).toBeVisible();
    // disabled sections stay visible but carry their enabling stage
    expect(screen.getByTitle("enabled by stage S6")).toHaveTextContent("S6");
  });

  it("routes a disabled section to the shared ComingSoon panel", async () => {
    renderWithProviders(<App />, { initialEntries: ["/workflows"] });

    await screen.findByText(/DAG runs reusing the same event model/);
    expect(screen.getByText(/enabled by stage/i)).toBeVisible();
    // the stage appears in both the nav badge and the panel — assert presence
    expect(screen.getAllByText("S6").length).toBeGreaterThan(0);
  });
});

describe("App shell 401 funnel (S2 gap fix)", () => {
  it("required mode + no principal → the sign-in screen replaces the shell", async () => {
    server.use(
      http.get("/v1/auth/whoami", () => new HttpResponse(null, { status: 401 })),
    );
    renderWithProviders(<App />, { initialEntries: ["/agents"] });

    // the sign-in surface, not raw 401 errors from every section
    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeVisible();
    expect(screen.queryByRole("heading", { name: /agents/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Executions/ })).not.toBeInTheDocument();
  });

  it("signing in lifts the gate — sections refetch and render", async () => {
    server.use(
      http.get("/v1/auth/whoami", () => new HttpResponse(null, { status: 401 })),
      http.post("/v1/auth/login", () => HttpResponse.json(whoamiFixture)),
    );
    const user = userEvent.setup();
    renderWithProviders(<App />, { initialEntries: ["/agents"] });

    await screen.findByRole("heading", { name: "Sign in" });
    await user.type(screen.getByLabelText("Email"), "owner@acme.test");
    await user.type(screen.getByLabelText("Password"), "hunter2");
    await user.click(screen.getByRole("button", { name: "Sign in" }));

    expect(await screen.findByRole("heading", { name: /agents/i })).toBeVisible();
  });
});

describe("SectionGate", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("shows an honest connecting state while the payload is pending", () => {
    vi.stubGlobal("fetch", () => new Promise(() => {}));

    renderWithProviders(
      <SectionGate sectionKey="agents">
        <div>should not render</div>
      </SectionGate>,
      { capabilities: null },
    );

    expect(screen.getByText(/connecting to the jarvis backend/i)).toBeVisible();
  });

  it("shows a retryable error state when the backend is unreachable", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.reject(new Error("network down"))),
    );

    renderWithProviders(
      <SectionGate sectionKey="agents">
        <div>should not render</div>
      </SectionGate>,
      { capabilities: null },
    );

    // the query retries once (exponential backoff ~1s) before surfacing
    // the error state, so allow a generous findBy timeout
    await screen.findByText(
      /cannot reach the jarvis backend/i,
      undefined,
      { timeout: 5000 },
    );
    expect(screen.getByRole("button", { name: "Retry" })).toBeVisible();
  });

  it("renders children for an enabled section", () => {
    renderWithProviders(
      <SectionGate sectionKey="agents">
        <div>agents content</div>
      </SectionGate>,
    );

    expect(screen.getByText("agents content")).toBeVisible();
  });

  it("tolerates a payload that lacks a known section (forward-compat)", () => {
    const partial = { sections: { agents: TEST_CAPABILITIES.sections.agents! } };

    renderWithProviders(
      <SectionGate sectionKey="settings">
        <div>settings content</div>
      </SectionGate>,
      { capabilities: partial },
    );

    expect(screen.getByText(/unknown section/i)).toBeVisible();
  });
});