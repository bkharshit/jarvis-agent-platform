import { http, HttpResponse } from "msw";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { apiKeyCreatedFixture, apiKeyFixture } from "@/test/handlers";
import { server } from "@/test/msw";
import { renderWithProviders } from "@/test/render";

import { ApiKeysPanel } from "./ApiKeysPanel";

describe("<ApiKeysPanel/>", () => {
  it("renders key metadata — prefix and status, never a plaintext", async () => {
    renderWithProviders(<ApiKeysPanel canCreate />, { initialEntries: ["/settings"] });

    expect(await screen.findByText("ci")).toBeInTheDocument();
    expect(screen.getByText(`${apiKeyFixture.key_prefix}…`)).toBeInTheDocument();
    expect(screen.getByText("active")).toBeInTheDocument();
    // no GET ever carries a plaintext — assert the create-time one-liner
    // ("jarvis_sk_" full key) is absent from the listing view
    expect(
      screen.queryByText(/jarvis_sk_[0-9a-f]{40,}/),
    ).not.toBeInTheDocument();
  });

  it("shows the plaintext exactly once at create, then only until dismissed", async () => {
    const user = userEvent.setup();
    renderWithProviders(<ApiKeysPanel canCreate />, { initialEntries: ["/settings"] });

    await user.type(await screen.findByPlaceholderText("key name"), "cli key");
    await user.click(screen.getByRole("button", { name: "Create key" }));

    const banner = await screen.findByRole("status");
    expect(banner).toHaveTextContent("copy it now");
    // the plaintext appears in the one-time banner only
    expect(banner.textContent).toContain(apiKeyCreatedFixture.plaintext);

    await user.click(screen.getByRole("button", { name: "Done — I saved it" }));
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("revokes an active key after confirmation", async () => {
    const user = userEvent.setup();
    let revoked = false;
    const revokeSpy = vi.fn(() => {
      revoked = true;
      return new HttpResponse(null, { status: 204 });
    });
    server.use(
      http.delete("/v1/api-keys/:key_id", () => revokeSpy()),
      // active until revoked; the refetch after invalidation then sees it
      http.get("/v1/api-keys", () =>
        HttpResponse.json({
          items: [
            { ...apiKeyFixture, revoked_at: revoked ? "2026-09-05T11:00:00Z" : null },
          ],
        }),
      ),
    );
    vi.spyOn(window, "confirm").mockReturnValue(true);

    renderWithProviders(<ApiKeysPanel canCreate />, { initialEntries: ["/settings"] });
    await user.click(await screen.findByRole("button", { name: "revoke" }));
    await screen.findByText("revoked");
    expect(revokeSpy).toHaveBeenCalledTimes(1);
  });

  it("hides mutating controls for a principal that cannot own keys", async () => {
    renderWithProviders(<ApiKeysPanel canCreate={false} />, { initialEntries: ["/settings"] });
    expect(await screen.findByText("ci")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Create key" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "revoke" })).not.toBeInTheDocument();
  });
});