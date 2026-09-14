import { expect, test } from "@playwright/test";

// One smoke spec over the live backend + mock provider (decision 11).
// Randomized agent name per run; rows are left behind on purpose.
// Prerequisite: `uv run jarvis serve` on :8000 with local Postgres.

const agentName = `e2e-smoke-${Date.now()}`;
const sessionId = `e2e-session-${Date.now()}`;
const EMAIL = process.env.E2E_EMAIL ?? "s6-owner@jarvis.test";
const PASSWORD = process.env.E2E_PASSWORD ?? "s3cret";

test("create → run → executions → replay → conversations → disabled sections", async ({
  page,
}) => {
  // 0. Auth mode: a required-auth instance shows the AuthGate login
  // screen on first navigation — sign in when it appears.
  await page.goto("/agents");
  const needsLogin = await page
    .getByLabel("Email")
    .isVisible()
    .catch(() => false);
  if (needsLogin) {
    await page.getByLabel("Email").fill(EMAIL);
    await page.getByLabel("Password").fill(PASSWORD);
    await page.getByRole("button", { name: "Sign in" }).click();
    await expect(page.getByText(agentName).or(page.getByRole("heading", { name: "Agents" }))).toBeVisible({ timeout: 20_000 });
  }

  // 1. Create a mock function_calling agent.
  await page.goto("/agents/new");
  await page.getByLabel("Name").fill(agentName);
  await page.getByLabel("Provider").selectOption("mock");
  await page.getByLabel("Model").fill("mock-agent");
  await page.getByLabel("Strategy", { exact: true }).selectOption("function_calling");
  // Memory on: the runtime creates the (agent, session) conversation row
  // only for memory-enabled agents, which the conversations step reads.
  await page.getByRole("checkbox", { name: "Enabled" }).check();
  await page.getByRole("button", { name: "Save" }).click();

  // 2. It appears in the list.
  await page.goto("/agents");
  await expect(page.getByText(agentName)).toBeVisible();

  // 3. Run console: live SSE against the mock provider.
  const agentId = await page.evaluate(async (name: string) => {
    const res = await fetch("/v1/agents");
    if (!res.ok) throw new Error(`agents list failed: ${res.status}`);
    const body = (await res.json()) as { items: { id: string; name: string }[] };
    const found = body.items.find((a) => a.name === name);
    if (!found) throw new Error("created agent not found in list");
    return found.id;
  }, agentName);
  await page.goto(`/agents/${agentId}/run`);
  await page.getByLabel("Run input").fill("What is 2+2? Answer briefly.");
  await page.getByPlaceholder("session id (optional)").fill(sessionId);
  await page.getByRole("button", { name: "Run" }).click();
  await expect(page.getByText("Finished")).toBeVisible();
  await expect(page.getByTestId("final-message")).toBeVisible();

  // 4. Executions list shows the run as succeeded; detail renders. The run
  // id isn't known up front — read it from the API like the UI does.
  const runId = await page.evaluate(async () => {
    const res = await fetch("/v1/executions");
    if (!res.ok) throw new Error(`executions list failed: ${res.status}`);
    const body = (await res.json()) as { items: { run_id: string; status: string }[] };
    const succeeded = body.items.find((r) => r.status === "succeeded");
    if (!succeeded) throw new Error("no succeeded run found");
    return succeeded.run_id;
  });
  await page.goto("/executions");
  await page.getByRole("button", { name: "succeeded" }).click();
  await expect(page.getByText(runId.slice(0, 12) + "…")).toBeVisible();
  await page.goto(`/executions/${runId}`);
  await expect(page.getByText(runId)).toBeVisible();
  await expect(page.getByText("Transcript")).toBeVisible();

  // 5. Replay folds the stored log through the same renderer.
  await page.getByRole("button", { name: "Replay events" }).click();
  await expect(page.getByTestId("replay-timeline")).toBeVisible();

  // 6. Conversations: the session index derives from executions; the
  // transcript comes from the real conversations endpoint.
  await page.goto("/conversations");
  await expect(page.getByText(sessionId)).toBeVisible();
  await page.getByText(sessionId).click();
  await expect(page.getByText(`agent ${agentId}`)).toBeVisible();

  // 7. Every disabled section names its stage — no fake content. Workflows
  // went live in S6 and settings in S2, so both render real sections;
  // the still-unbuilt stages keep their honest gates.
  await page.goto("/workflows");
  await expect(page.getByRole("heading", { name: "Workflows" })).toBeVisible();
  await page.goto("/knowledge");
  await expect(page.getByText("Coming soon — enabled by stage")).toBeVisible();
  await expect(page.getByRole("main").getByText("S8")).toBeVisible();
});