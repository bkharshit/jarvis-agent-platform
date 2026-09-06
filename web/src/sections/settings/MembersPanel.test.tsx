import { http, HttpResponse } from "msw";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { envelope, memberFixture } from "@/test/handlers";
import { server } from "@/test/msw";
import { renderWithProviders } from "@/test/render";

import { MembersPanel } from "./MembersPanel";

describe("<MembersPanel/>", () => {
  it("renders members from the API payload", async () => {
    renderWithProviders(<MembersPanel canManage />, { initialEntries: ["/settings"] });

    expect(await screen.findByText("member@acme.test")).toBeInTheDocument();
    expect(screen.getByText("member")).toBeInTheDocument();
  });

  it("creates a member (keys-only when the password is blank)", async () => {
    const user = userEvent.setup();
    let sent: Record<string, unknown> = {};
    server.use(
      http.post("/v1/members", async ({ request }) => {
        sent = (await request.json()) as Record<string, string>;
        return HttpResponse.json(memberFixture, { status: 201 });
      }),
    );

    renderWithProviders(<MembersPanel canManage />, { initialEntries: ["/settings"] });
    await user.click(await screen.findByRole("button", { name: "Invite member" }));
    await user.type(screen.getByLabelText("Email"), "new@acme.test");
    await user.selectOptions(screen.getByLabelText("Role"), "admin");
    await user.click(screen.getByRole("button", { name: "Create member" }));

    await screen.findByRole("button", { name: "Invite member" });
    expect(sent.email).toBe("new@acme.test");
    expect(sent.role).toBe("admin");
    expect(sent.password).toBeUndefined(); // blank password = keys-only member
  });

  it("surfaces the API's 403 verbatim when the caller is not an admin/owner", async () => {
    server.use(
      http.get("/v1/members", () =>
        HttpResponse.json(
          envelope("forbidden", "member management requires the admin or owner role"),
          { status: 403 },
        ),
      ),
    );
    renderWithProviders(<MembersPanel canManage={false} />, { initialEntries: ["/settings"] });
    expect(
      await screen.findByText(
        "member management requires the admin or owner role",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Invite member" })).not.toBeInTheDocument();
  });

  it("removes a member after confirmation", async () => {
    const user = userEvent.setup();
    let removed = false;
    const deleteSpy = vi.fn(() => {
      removed = true;
      return new HttpResponse(null, { status: 204 });
    });
    server.use(
      http.delete("/v1/members/:user_id", () => deleteSpy()),
      http.get("/v1/members", () =>
        HttpResponse.json({ items: removed ? [] : [memberFixture] }),
      ),
    );
    vi.spyOn(window, "confirm").mockReturnValue(true);

    renderWithProviders(<MembersPanel canManage />, { initialEntries: ["/settings"] });
    await user.click(await screen.findByRole("button", { name: "remove" }));
    await screen.findByText("No members listed.");
    expect(deleteSpy).toHaveBeenCalledTimes(1);
  });
});