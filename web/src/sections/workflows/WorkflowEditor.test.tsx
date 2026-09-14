import { http, HttpResponse } from "msw";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { envelope } from "@/test/handlers";
import { server } from "@/test/msw";
import { TEST_CAPABILITIES, renderWithProviders } from "@/test/render";

import { WorkflowEditorPage } from "./WorkflowEditor";

const LIVE = {
  ...TEST_CAPABILITIES,
  sections: {
    ...TEST_CAPABILITIES.sections,
    workflows: { enabled: true, summary: "DAG runs reusing the same event model" },
  },
};

const detailFixture = {
  definition: {
    id: "wf-1",
    name: "chain",
    description: "",
    nodes: [
      {
        id: "a",
        type: "agent",
        config: { agent_id: "agent-1", agent_version_id: "ver-9", input_template: "{{input}}" },
      },
    ],
    edges: [],
    start_node_id: "a",
    max_node_executions: 24,
    created_at: "2026-09-14T00:00:00Z",
    updated_at: "2026-09-14T00:00:00Z",
  },
  versions: [{ version: 1, id: "wver-1", label: "initial", created_at: "2026-09-14T00:00:00Z" }],
  lints: [],
};

function detailResponse(definition = detailFixture.definition) {
  return { ...detailFixture, definition };
}

describe("<WorkflowEditorPage/>", () => {
  it("blocks an empty graph with validation, then saves a stripped draft (create)", async () => {
    const user = userEvent.setup();
    let posts = 0;
    let posted: Request | null = null;
    server.use(
      http.post("/v1/workflows", ({ request }) => {
        posts += 1;
        posted = request;
        return HttpResponse.json(detailFixture, { status: 201 });
      }),
    );
    server.use(
      http.get("/v1/agents", () =>
        HttpResponse.json({ items: [{ id: "agent-1", name: "Research agent" }] }),
      ),
    );

    renderWithProviders(<WorkflowEditorPage />, { capabilities: LIVE });

    // empty graph → client-side validation, no POST
    await user.click(await screen.findByTestId("save-workflow"));
    expect(await screen.findByTestId("save-error")).toHaveTextContent("add at least one node");
    expect(posts).toBe(0);

    // palette adds an agent node
    await user.click(screen.getByRole("button", { name: "+ agent" }));
    await waitFor(() => expect(screen.getByTestId("node-panel-agent")).toBeInTheDocument());

    await user.type(screen.getByLabelText("Workflow name"), "my flow");
    await user.click(screen.getByTestId("save-workflow"));
    await waitFor(() => expect(posts).toBe(1));

    const body = (await posted!.json()) as {
      name: string;
      nodes: { id: string; type: string; config: Record<string, unknown> }[];
      start_node_id: string;
      max_node_executions: number;
    };
    expect(body.name).toBe("my flow");
    expect(body.start_node_id).toBe("agent-1");
    expect(body.max_node_executions).toBe(24);
    expect(body.nodes).toHaveLength(1);
    // the save-time `_` strip: the payload never carries UI-only keys
    expect(Object.keys(body.nodes[0].config).some((k) => k.startsWith("_"))).toBe(false);
  });

  it("guards a concurrent edit with the server hash and refuses to clobber", async () => {
    const user = userEvent.setup();
    let fetches = 0;
    server.use(
      http.get("/v1/workflows/:workflow_id", () => {
        fetches += 1;
        // the second read (the save-time guard) sees a CHANGED definition
        const changed = {
          ...detailFixture.definition,
          description: "edited elsewhere",
        };
        return HttpResponse.json(detailResponse(fetches > 1 ? changed : undefined));
      }),
    );
    let patches = 0;
    server.use(
      http.patch("/v1/workflows/:workflow_id", () => {
        patches += 1;
        return HttpResponse.json(detailFixture);
      }),
    );

    renderWithProviders(<WorkflowEditorPage />, {
      capabilities: LIVE,
      initialEntries: ["/workflows/wf-1"],
      path: "/workflows/:workflowId",
    });
    await screen.findByDisplayValue("chain");

    await user.click(screen.getByTestId("save-workflow"));
    expect(await screen.findByTestId("save-error")).toHaveTextContent(
      /changed on the server/,
    );
    // the conflict guard fired before the PATCH — nothing was overwritten
    expect(patches).toBe(0);
  });

  it("saves through the server hash when unchanged and PATCHes the next version", async () => {
    const user = userEvent.setup();
    server.use(http.get("/v1/workflows/:workflow_id", () => HttpResponse.json(detailResponse())));
    let patches = 0;
    let patched: Request | null = null;
    server.use(
      http.patch("/v1/workflows/:workflow_id", ({ request }) => {
        patches += 1;
        patched = request;
        return HttpResponse.json(detailResponse());
      }),
    );

    renderWithProviders(<WorkflowEditorPage />, {
      capabilities: LIVE,
      initialEntries: ["/workflows/wf-1"],
      path: "/workflows/:workflowId",
    });
    await screen.findByDisplayValue("chain");

    await user.click(screen.getByTestId("save-workflow"));
    await waitFor(() => expect(patches).toBe(1));
    const body = (await patched!.json()) as {
      nodes: { id: string }[];
      start_node_id: string;
    };
    expect(body.start_node_id).toBe("a");
    // the saved pin rides along (D42 re-pins server-side at publish)
    expect(body.nodes[0]?.id).toBe("a");
  });

  it("surfaces the server's message when create conflicts (409)", async () => {
    const user = userEvent.setup();
    server.use(
      http.post("/v1/workflows", () =>
        HttpResponse.json(envelope("conflict", "workflow name 'x' already exists"), { status: 409 }),
      ),
    );

    renderWithProviders(<WorkflowEditorPage />, { capabilities: LIVE });
    await user.click(screen.getByRole("button", { name: "+ agent" }));
    await user.type(screen.getByLabelText("Workflow name"), "dup");
    await user.click(screen.getByTestId("save-workflow"));
    expect(await screen.findByTestId("save-error")).toHaveTextContent("already exists");
  });
});