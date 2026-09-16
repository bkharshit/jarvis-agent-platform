import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import { renderWithProviders, TEST_CAPABILITIES } from "@/test/render";
import { server } from "@/test/msw";
import { envelope, triggerFixture } from "@/test/handlers";

import { TriggersPage } from "./TriggersPage";

// The triggers section (S13): list + create form (per-kind config), the
// webhook secret's plaintext shown EXACTLY ONCE (create / rotate), fire-now,
// toggle, delete, and the runs link carrying ?trigger= to Executions.

describe("<TriggersPage/>", () => {
  it("renders triggers from the API payload (list, kind, schedule, last error)", async () => {
    renderWithProviders(<TriggersPage />, { initialEntries: ["/triggers"] });
    expect(await screen.findByText("nightly-digest")).toBeInTheDocument();
    expect(screen.getByText("alerts-hook")).toBeInTheDocument();
    expect(screen.getByText("0 3 * * *")).toBeInTheDocument();
    expect(screen.getAllByText("enabled").length).toBeGreaterThan(0);
    // the webhook row derives its ingest URL client-side
    expect(screen.getByText(/\/v1\/triggers\/webhook\/trg-2/)).toBeInTheDocument();
  });

  it("shows the honest last_error on the row", async () => {
    server.use(
      http.get("/v1/triggers", () =>
        HttpResponse.json({
          items: [
            {
              ...triggerFixture,
              last_error: "target agent 'x' not found with no published version",
            },
          ],
        }),
      ),
    );
    renderWithProviders(<TriggersPage />, { initialEntries: ["/triggers"] });
    await screen.findByText("nightly-digest");
    expect(screen.getByText(/last error: target agent/)).toBeInTheDocument();
  });

  it("renders the coming-soon panel naming the stage when the gate is off", async () => {
    renderWithProviders(<TriggersPage />, {
      initialEntries: ["/triggers"],
      capabilities: {
        ...TEST_CAPABILITIES,
        sections: {
          ...TEST_CAPABILITIES.sections,
          triggers: { enabled: false, stage: "S13", summary: "Cron, webhook, and event rules" },
        },
      },
    });
    // ComingSoon's shared panel: the summary line + the stage badge.
    expect(await screen.findByText("Cron, webhook, and event rules")).toBeInTheDocument();
    expect(screen.getByText(/Coming soon — enabled by stage/)).toBeInTheDocument();
    expect(screen.getByText("S13")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Add trigger" })).not.toBeInTheDocument();
  });

  it("fires now and reports the queued run", async () => {
    const user = userEvent.setup();
    let fired = 0;
    server.use(
      http.post("/v1/triggers/:trigger_id/fire", () => {
        fired += 1;
        return HttpResponse.json({ run_id: "run-9" }, { status: 202 });
      }),
    );
    renderWithProviders(<TriggersPage />, { initialEntries: ["/triggers"] });
    await screen.findByText("nightly-digest");
    await user.click(screen.getAllByTestId("fire-now")[0]);
    await waitFor(() => expect(fired).toBe(1));
    expect(await screen.findByText(/Fired nightly-digest/)).toBeInTheDocument();
  });

  it("shows the webhook secret exactly once on create and never again", async () => {
    const user = userEvent.setup();
    renderWithProviders(<TriggersPage />, { initialEntries: ["/triggers"] });
    await user.click(await screen.findByRole("button", { name: "Add trigger" }));
    await user.type(screen.getByLabelText("Name"), "hook");
    await user.selectOptions(screen.getByLabelText("Kind"), "webhook");
    await user.selectOptions(screen.getByLabelText("Target agent"), "agent-1");
    await user.click(screen.getByRole("button", { name: "Create trigger" }));
    const once = await screen.findByTestId("secret-once");
    expect(once).toHaveTextContent("whsec_only-shown-at-create-time");
    expect(once).toHaveTextContent("only time it is shown");
    // dismissing is the end — the plaintext never comes back from a GET
    await user.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(screen.queryByTestId("secret-once")).not.toBeInTheDocument();
  });

  it("rotates a webhook secret and shows the new plaintext once", async () => {
    const user = userEvent.setup();
    renderWithProviders(<TriggersPage />, { initialEntries: ["/triggers"] });
    await screen.findByText("alerts-hook");
    await user.click(screen.getByRole("button", { name: "Rotate secret" }));
    const once = await screen.findByTestId("secret-once");
    expect(once).toHaveTextContent("whsec_rotated-only-shown-once");
  });

  it("links the runs-of-trigger list with the trigger filter", async () => {
    renderWithProviders(<TriggersPage />, { initialEntries: ["/triggers"] });
    const links = await screen.findAllByRole("link", { name: "Runs" });
    expect(links[0]).toHaveAttribute("href", "/executions?trigger=trg-1");
  });

  it("disables a trigger with a full-document PATCH", async () => {
    const user = userEvent.setup();
    let patched: Record<string, unknown> | null = null;
    server.use(
      http.patch("/v1/triggers/:trigger_id", async ({ request }) => {
        patched = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({ trigger: { ...triggerFixture, enabled: false }, lints: [] });
      }),
    );
    renderWithProviders(<TriggersPage />, { initialEntries: ["/triggers"] });
    await screen.findByText("nightly-digest");
    await user.click(screen.getAllByRole("button", { name: "Disable" })[0]);
    await waitFor(() => expect(patched).not.toBeNull());
    // FULL document: every field present (the PATCH discipline)
    expect(patched).toMatchObject({
      name: triggerFixture.name,
      kind: triggerFixture.kind,
      agent_id: triggerFixture.agent_id,
      version_policy: triggerFixture.version_policy,
      enabled: false,
      config: triggerFixture.config,
    });
  });

  it("surfaces a create 422 honestly", async () => {
    const user = userEvent.setup();
    server.use(
      http.post(
        "/v1/triggers",
        () =>
          HttpResponse.json(
            envelope("validation", "trigger target agent 'x' has no published version"),
            { status: 422 },
          ),
      ),
    );
    renderWithProviders(<TriggersPage />, { initialEntries: ["/triggers"] });
    await user.click(await screen.findByRole("button", { name: "Add trigger" }));
    await user.type(screen.getByLabelText("Name"), "doomed");
    await user.selectOptions(screen.getByLabelText("Target agent"), "agent-1");
    await user.click(screen.getByRole("button", { name: "Create trigger" }));
    // The message lands twice (toast + the inline form alert); assert the
    // form's role="alert" specifically — the toast renders role="status".
    expect(await screen.findByRole("alert")).toHaveTextContent(/no published version/);
  });

  it("hides mutating controls for a signed-in member (role gate parity with the API)", async () => {
    server.use(
      http.get("/v1/auth/whoami", () =>
        HttpResponse.json({
          tenant_id: "default",
          mode: "session",
          user_id: "user-2",
          email: "member@acme.test",
          display_name: "Member Two",
          role: "member",
        }),
      ),
    );
    renderWithProviders(<TriggersPage />, { initialEntries: ["/triggers"] });
    await screen.findByText("nightly-digest");
    expect(screen.queryByRole("button", { name: "Add trigger" })).not.toBeInTheDocument();
    expect(screen.queryAllByTestId("fire-now")).toHaveLength(0);
    expect(screen.getAllByRole("link", { name: "Runs" }).length).toBeGreaterThan(0);
  });
});