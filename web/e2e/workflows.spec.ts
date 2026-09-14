import { expect, test, type Page } from "@playwright/test";

// S6 workflows e2e over the live backend (decision 11): capability flip,
// canvas authoring with the server-hash save, the run console's node
// groups (D43), the executions names map (D41), and delete 409/204.
// Mock provider → deterministic ("This is a mock response.").
//
// Prerequisites: `uv run jarvis serve` on the Vite proxy target with
// auth_mode=required, and a walkthrough owner user —
//   uv run jarvis user create default s6-owner@jarvis.test \
//     --role owner --password s3cret
//   uv run jarvis api-key create s6-owner@jarvis.test --name e2e  (unused,
//     the spec logs in through the UI and rides the session cookie)
// Override credentials with E2E_EMAIL / E2E_PASSWORD.

const EMAIL = process.env.E2E_EMAIL ?? "s6-owner@jarvis.test";
const PASSWORD = process.env.E2E_PASSWORD ?? "s3cret";
const suffix = Date.now();
const agentName = `s6-e2e-agent-${suffix}`;
const workflowName = `s6-e2e-${suffix}`;

async function login(page: Page): Promise<void> {
  await page.goto("/workflows");
  await page.getByLabel("Email").fill(EMAIL);
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { name: "Workflows" })).toBeVisible();
}

async function createMockAgent(page: Page): Promise<string> {
  await page.goto("/agents/new");
  await page.getByLabel("Name").fill(agentName);
  await page.getByLabel("Provider").selectOption("mock");
  await page.getByLabel("Model").fill("mock-agent");
  await page.getByLabel("Strategy", { exact: true }).selectOption("function_calling");
  await page.getByRole("button", { name: "Save" }).click();
  await page.goto("/agents");
  await expect(page.getByText(agentName)).toBeVisible();
  return page.evaluate(async (name: string) => {
    const res = await fetch("/v1/agents");
    if (!res.ok) throw new Error(`agents list failed: ${res.status}`);
    const body = (await res.json()) as { items: { id: string; name: string }[] };
    const found = body.items.find((a) => a.name === name);
    if (!found) throw new Error("created agent not found in list");
    return found.id;
  }, agentName);
}

test("author → save → run → node group → names → delete 409/204", async ({
  page,
}) => {
  await login(page);
  const agentId = await createMockAgent(page);

  // --- author: canvas + editor save (D42 pin stamped server-side) ---------
  await page.goto("/workflows");
  await page.getByTestId("new-workflow").click();
  await page.getByRole("button", { name: "+ agent" }).click();
  await expect(page.getByTestId("node-panel-agent")).toBeVisible();
  // the picker lists live agents; picking one shows the D42 pin note
  await page.getByLabel("Agent").selectOption(agentId);
  await expect(page.getByTestId("pin-display")).toContainText("pins the agent");
  await page.getByLabel("Workflow name").fill(workflowName);

  // empty-name and empty-graph validation came from the palette; save
  // publishes v1 and pins, landing on the editor page for the saved graph.
  await page.getByTestId("save-workflow").click();
  await page.waitForURL(/\/workflows\/[0-9a-f-]{36}/);

  // --- run: the console streams node events into node groups (D43) --------
  await page.getByTestId("run-link").click();
  await page.getByLabel("Run input").fill("walk me");
  await page.getByRole("button", { name: "Run", exact: true }).click();
  // the node's group card renders with its inner events nested
  await expect(page.getByTestId("node-group-agent-1")).toBeVisible();
  await expect(page.getByTestId("final-message")).toContainText(
    "This is a mock response.",
  );

  // --- executions: the run names its WORKFLOW (D41 + names map) -----------
  await page.goto("/executions");
  await expect(page.getByText(workflowName).first()).toBeVisible();

  // --- delete: executed workflow refuses; fresh one deletes (204) --------
  await page.goto("/workflows");
  page.on("dialog", (dialog) => void dialog.accept());
  await page
    .locator("li", { has: page.getByRole("link", { name: workflowName }) })
    .getByRole("button", { name: "Delete" })
    .click();
  await expect(page.getByText(/delete refused/)).toBeVisible();

  // a fresh workflow (no executions) deletes cleanly
  const created = await page.evaluate(async (agentId: string) => {
    const res = await fetch("/v1/workflows", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: `s6-e2e-fresh-${Date.now()}`,
        nodes: [
          {
            id: "a",
            type: "agent",
            config: { agent_id: agentId, agent_version_id: null, input_template: "{{input}}" },
          },
        ],
        edges: [],
        start_node_id: "a",
      }),
    });
    if (!res.ok) throw new Error(`workflow create failed: ${res.status}`);
    return ((await res.json()) as { definition: { id: string } }).definition.id;
  }, agentId);
  await page.goto("/workflows");
  const freshRow = page.locator("li", {
    has: page.locator(`a[href="/workflows/${created}"]`),
  });
  await expect(freshRow).toBeVisible();
  await freshRow.getByRole("button", { name: "Delete" }).click();
  await expect(freshRow).toHaveCount(0);
});

test("concurrent-edit guard: a stale editor refuses to clobber", async ({
  page,
}) => {
  await login(page);
  // page.evaluate serializes the callback — the agent name rides in as an
  // argument, it cannot be a closure.
  const agentId = await page.evaluate(async (name: string) => {
    const res = await fetch("/v1/agents");
    const body = (await res.json()) as { items: { id: string; name: string }[] };
    const found = body.items.find((a) => a.name === name);
    if (!found) throw new Error("run the authoring test first (it creates the agent)");
    return found.id;
  }, agentName);

  // a workflow to fight over
  const workflowId = await page.evaluate(async (agentId: string) => {
    const res = await fetch("/v1/workflows", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: `s6-e2e-guard-${Date.now()}`,
        nodes: [
          {
            id: "a",
            type: "agent",
            config: { agent_id: agentId, agent_version_id: null, input_template: "{{input}}" },
          },
        ],
        edges: [],
        start_node_id: "a",
      }),
    });
    if (!res.ok) throw new Error(`workflow create failed: ${res.status}`);
    return ((await res.json()) as { definition: { id: string } }).definition.id;
  }, agentId);

  // two tabs (one context → shared session), both editing the same graph
  const page2 = await page.context().newPage();
  await page.goto(`/workflows/${workflowId}`);
  await page2.goto(`/workflows/${workflowId}`);
  await expect(page.getByLabel("Workflow name")).toHaveValue(/s6-e2e-guard/);
  await expect(page2.getByLabel("Workflow name")).toHaveValue(/s6-e2e-guard/);

  // tab 1 saves (bumps the definition; a new version publishes)
  await page.getByTestId("save-workflow").click();
  await expect(page2.getByTestId("save-error")).toBeHidden(); // precondition

  // tab 2's stale hash must refuse — surfaced, never a silent overwrite
  await page2.getByTestId("save-workflow").click();
  await expect(page2.getByTestId("save-error")).toContainText(
    "changed on the server",
  );
  await page2.close();
});