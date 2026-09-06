import { http, HttpResponse } from "msw";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { credentialFixture } from "@/test/handlers";
import { server } from "@/test/msw";
import { renderWithProviders } from "@/test/render";

import { CredentialsPanel } from "./CredentialsPanel";

describe("<CredentialsPanel/>", () => {
  it("renders credential metadata only — no secret ever comes back", async () => {
    renderWithProviders(<CredentialsPanel canManage storageAvailable />, {
      initialEntries: ["/settings"],
    });

    expect(await screen.findByText("prod key")).toBeInTheDocument();
    expect(screen.getByText("openai_compatible")).toBeInTheDocument();
    expect(screen.getByText("active")).toBeInTheDocument();
    // write-only surface (ADR 0006 §7): nothing secret-shaped renders
    expect(screen.queryByText(/sk-/)).not.toBeInTheDocument();
  });

  it("creates a credential — the secret is sent once and never echoed back", async () => {
    const user = userEvent.setup();
    let sent: Record<string, string> = {};
    server.use(
      http.post("/v1/credentials", async ({ request }) => {
        sent = (await request.json()) as Record<string, string>;
        return HttpResponse.json(credentialFixture, { status: 201 });
      }),
    );

    renderWithProviders(<CredentialsPanel canManage storageAvailable />, {
      initialEntries: ["/settings"],
    });
    await user.click(await screen.findByRole("button", { name: "Add credential" }));
    await user.type(screen.getByLabelText("Name"), "prod key");
    await user.type(screen.getByLabelText("Provider"), "openai_compatible");
    await user.type(screen.getByLabelText("Secret"), "sk-e2e-material-123");
    await user.click(screen.getByRole("button", { name: "Save credential" }));

    await screen.findByRole("button", { name: "Add credential" });
    expect(sent.secret).toBe("sk-e2e-material-123");
    // and it never renders after the create response returns
    expect(screen.queryByText("sk-e2e-material-123")).not.toBeInTheDocument();
  });

  it("replaces a secret in place (write-only PATCH)", async () => {
    const user = userEvent.setup();
    let sent: Record<string, string> = {};
    server.use(
      http.patch("/v1/credentials/:credential_id", async ({ request }) => {
        sent = (await request.json()) as Record<string, string>;
        return HttpResponse.json(credentialFixture);
      }),
    );

    renderWithProviders(<CredentialsPanel canManage storageAvailable />, {
      initialEntries: ["/settings"],
    });
    await user.click(await screen.findByRole("button", { name: "replace secret" }));
    await user.type(screen.getByPlaceholderText("new secret"), "sk-rotated-999");
    await user.click(screen.getByRole("button", { name: "save" }));

    expect(sent).toEqual({ secret: "sk-rotated-999" });
    expect(screen.queryByText("sk-rotated-999")).not.toBeInTheDocument();
  });

  it("warns when BYOK storage is not configured on the backend", async () => {
    renderWithProviders(<CredentialsPanel canManage storageAvailable={false} />, {
      initialEntries: ["/settings"],
    });
    expect(
      await screen.findByText(/BYOK storage is not configured/),
    ).toBeInTheDocument();
  });
});