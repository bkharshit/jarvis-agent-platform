import { http, HttpResponse } from "msw";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { envelope, knowledgeDatasetFixture, knowledgeDatasetsFixture } from "@/test/handlers";
import { server } from "@/test/msw";
import { renderWithProviders } from "@/test/render";

import KnowledgePage from "./KnowledgePage";

describe("<KnowledgePage/>", () => {
  it("renders datasets with their pinned embedding configs", async () => {
    renderWithProviders(<KnowledgePage />);
    expect(await screen.findByRole("link", { name: "Product notes" })).toBeInTheDocument();
    expect(screen.getByText("mock/mock-embedder")).toBeInTheDocument();
    expect(screen.getByText("dim 8")).toBeInTheDocument();
  });

  it("shows an honest empty state", async () => {
    server.use(
      http.get("/v1/knowledge/datasets", () => HttpResponse.json({ items: [] })),
    );
    renderWithProviders(<KnowledgePage />);
    expect(await screen.findByTestId("no-datasets")).toBeInTheDocument();
  });

  it("creates a dataset sending the pinned embedding config", async () => {
    const user = userEvent.setup();
    const bodies: unknown[] = [];
    server.use(
      http.post("/v1/knowledge/datasets", async ({ request }) => {
        bodies.push(await request.json());
        return HttpResponse.json(
          { ...knowledgeDatasetFixture, id: "kb-ds-new" },
          { status: 201 },
        );
      }),
    );
    renderWithProviders(<KnowledgePage />);
    await user.type(await screen.findByTestId("dataset-name-input"), "release docs");
    await user.type(screen.getByTestId("embedding-model-input"), "qwen3-embedding:0.6b");
    await user.type(screen.getByTestId("embedding-base-url-input"), "http://127.0.0.1:11434/v1");
    await user.click(screen.getByRole("button", { name: "Create dataset" }));

    await waitFor(() => expect(bodies).toHaveLength(1));
    expect(bodies[0]).toMatchObject({
      name: "release docs",
      embedding_config: {
        provider: "mock",
        model: "qwen3-embedding:0.6b",
        base_url: "http://127.0.0.1:11434/v1",
        dimension: 8,
        distance: "cosine",
      },
    });
  });

  it("filters datasets by the search box (name, case-insensitive)", async () => {
    const user = userEvent.setup();
    renderWithProviders(<KnowledgePage />);

    await screen.findByRole("link", { name: "Product notes" });
    await user.type(screen.getByLabelText("Search"), "product");
    expect(screen.getByRole("link", { name: "Product notes" })).toBeInTheDocument();

    await user.clear(screen.getByLabelText("Search"));
    await user.type(screen.getByLabelText("Search"), "no-such-dataset");
    expect(await screen.findByText("No datasets match")).toBeInTheDocument();
  });

  it("relays the delete-while-referenced 409 in the toast", async () => {
    const user = userEvent.setup();
    server.use(
      http.get("/v1/knowledge/datasets", () =>
        HttpResponse.json({
          items: [
            {
              ...knowledgeDatasetsFixture[0],
              id: "kb-ds-409",
              name: "pinned dataset",
            },
          ],
        }),
      ),
      http.delete("/v1/knowledge/datasets/kb-ds-409", () =>
        HttpResponse.json(
          envelope(
            "conflict",
            "knowledge dataset 'kb-ds-409' is referenced by 2 agent version snapshot(s); delete refused",
          ),
          { status: 409 },
        ),
      ),
    );
    renderWithProviders(<KnowledgePage />);
    const row = await screen.findByTestId("dataset-row");
    await user.click(row.querySelector("button")!);
    expect(await screen.findByText(/referenced by 2 agent version snapshot/)).toBeInTheDocument();
  });
});
