import { http, HttpResponse } from "msw";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { envelope, whoamiFixture } from "@/test/handlers";
import { server } from "@/test/msw";
import { renderWithProviders, TEST_CAPABILITIES } from "@/test/render";

import { SettingsPage } from "./SettingsPage";

const ANONYMOUS_CAPABILITIES = {
  ...TEST_CAPABILITIES,
  sections: {
    ...TEST_CAPABILITIES.sections,
    settings: {
      enabled: true,
      summary: "Auth, tenants, API keys, BYOK credentials",
      detail: { auth_mode: "anonymous", credentials: { available: true } },
    },
  },
};

const NO_MASTER_KEY_CAPABILITIES = {
  ...TEST_CAPABILITIES,
  sections: {
    ...TEST_CAPABILITIES.sections,
    settings: {
      enabled: true,
      summary: "Auth, tenants, API keys, BYOK credentials",
      detail: { auth_mode: "required", credentials: { available: false } },
    },
  },
};

describe("<SettingsPage/>", () => {
  it("renders the sign-in surface when auth is required and nobody is acting", async () => {
    server.use(
      http.get("/v1/auth/whoami", () =>
        HttpResponse.json(
          envelope("unauthenticated", "authentication required — log in or present an API key"),
          { status: 401 },
        ),
      ),
    );
    renderWithProviders(<SettingsPage />, { initialEntries: ["/settings"] });

    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
    expect(screen.getByText(/requires authentication/i)).toBeInTheDocument();
    // no panels leak through before sign-in
    expect(screen.queryByLabelText("Members")).not.toBeInTheDocument();
  });

  it("signs in through the login form and shows the panels after", async () => {
    const user = userEvent.setup();
    // stateful stand-in: unauthenticated until login, session after —
    // mirroring the cookie flip the real backend does
    let signedIn = false;
    server.use(
      http.get("/v1/auth/whoami", () =>
        signedIn
          ? HttpResponse.json(whoamiFixture)
          : HttpResponse.json(
              envelope("unauthenticated", "authentication required"),
              { status: 401 },
            ),
      ),
      http.post("/v1/auth/login", () => {
        signedIn = true;
        return HttpResponse.json(whoamiFixture);
      }),
    );
    renderWithProviders(<SettingsPage />, { initialEntries: ["/settings"] });

    await screen.findByRole("heading", { name: "Sign in" });
    await user.type(screen.getByLabelText("Email"), "owner@acme.test");
    await user.type(screen.getByLabelText("Password"), "pw");
    await user.click(screen.getByRole("button", { name: "Sign in" }));

    // login flips the whoami fact → the signed-in view replaces the form
    expect(await screen.findByText("owner@acme.test")).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Members" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Sign out" })).toBeInTheDocument();
  });

  it("shows the signed-in whoami summary, member management, and panels", async () => {
    renderWithProviders(<SettingsPage />, { initialEntries: ["/settings"] });

    expect(await screen.findByText("owner@acme.test")).toBeInTheDocument();
    expect(screen.getByText("default")).toBeInTheDocument();
    expect(screen.getByText("session")).toBeInTheDocument();
    // owner role → member management controls are live
    expect(screen.getByRole("button", { name: "Invite member" })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "JARVIS API keys" })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Provider LLM Credentials (BYOK)" })).toBeInTheDocument();
  });

  it("says anonymous mode out loud and hides mutating controls", async () => {
    const user = userEvent.setup();
    renderWithProviders(<SettingsPage />, {
      initialEntries: ["/settings"],
      capabilities: ANONYMOUS_CAPABILITIES,
    });

    expect(
      await screen.findByText(/Single-user local mode/),
    ).toBeInTheDocument();
    expect(screen.getByText(/JARVIS_AUTH_MODE=anonymous/)).toBeInTheDocument();
    // no sign-out for the anonymous principal; no member invite
    expect(screen.queryByRole("button", { name: "Sign out" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Invite member" })).not.toBeInTheDocument();
    // but the panels still read their data honestly
    expect(await screen.findByRole("region", { name: "JARVIS API keys" })).toBeInTheDocument();
    expect(user).toBeDefined();
  });

  it("warns when BYOK storage is not configured (the backend's own fact)", async () => {
    renderWithProviders(<SettingsPage />, {
      initialEntries: ["/settings"],
      capabilities: NO_MASTER_KEY_CAPABILITIES,
    });
    expect(
      await screen.findByText(/BYOK storage is not configured/),
    ).toBeInTheDocument();
  });

  it("signs out and drops to the login surface", async () => {
    const user = userEvent.setup();
    server.use(
      http.post("/v1/auth/logout", () => new HttpResponse(null, { status: 204 })),
    );
    renderWithProviders(<SettingsPage />, { initialEntries: ["/settings"] });

    await user.click(await screen.findByRole("button", { name: "Sign out" }));
    // logout clears the whoami fact → the required-mode page flips to login
    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
  });
});