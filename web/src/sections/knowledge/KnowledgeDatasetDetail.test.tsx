import { http, HttpResponse } from "msw";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { envelope, knowledgeDocumentsFixture } from "@/test/handlers";
import { server } from "@/test/msw";
import { renderWithProviders } from "@/test/render";

import KnowledgeDatasetDetail from "./KnowledgeDatasetDetail";

describe("<KnowledgeDatasetDetail/>", () => {
  it("renders the dataset's pinned config and its documents", async () => {
    renderWithProviders(<KnowledgeDatasetDetail />, {
      path: "/knowledge/:datasetId",
      initialEntries: ["/knowledge/kb-ds-1"],
    });
    expect(await screen.findByRole("heading", { name: "Product notes" })).toBeInTheDocument();
    expect(screen.getByText(/dim 8/)).toBeInTheDocument();
    const rows = await screen.findAllByTestId("document-row");
    expect(rows).toHaveLength(2);
    expect(screen.getByText("1 chunks")).toBeInTheDocument();
    expect(screen.getByText("0 chunks")).toBeInTheDocument();
  });

  it("shows the honest error row for a failed ingest", async () => {
    renderWithProviders(<KnowledgeDatasetDetail />, {
      path: "/knowledge/:datasetId",
      initialEntries: ["/knowledge/kb-ds-1"],
    });
    await screen.findAllByTestId("document-row");
    const errored = screen
      .getAllByTestId("document-status")
      .find((el) => el.textContent === "error");
    expect(errored).toBeInTheDocument();
    expect(await screen.findByTestId("document-error")).toHaveTextContent(
      "ModelConnectionError: connect ECONNREFUSED",
    );
  });

  it("ingests a pasted document (synchronous, 201 ready)", async () => {
    const user = userEvent.setup();
    const bodies: unknown[] = [];
    server.use(
      http.post("/v1/knowledge/datasets/:dataset_id/documents", async ({ request }) => {
        bodies.push(await request.json());
        return HttpResponse.json(
          {
            ...knowledgeDocumentsFixture[0],
            id: "kb-doc-new",
            name: "handbook.md",
          },
          { status: 201 },
        );
      }),
    );
    renderWithProviders(<KnowledgeDatasetDetail />, {
      path: "/knowledge/:datasetId",
      initialEntries: ["/knowledge/kb-ds-1"],
    });
    await user.type(await screen.findByTestId("document-name-input"), "handbook");
    await user.type(screen.getByTestId("document-content-input"), "The handbook says hello.");
    await user.click(screen.getByRole("button", { name: "Ingest document" }));

    await waitFor(() => expect(bodies).toHaveLength(1));
    expect(bodies[0]).toEqual({ name: "handbook", content: "The handbook says hello." });
  });

  it("relays an ingest failure (502 embedding_unavailable) in the form alert", async () => {
    const user = userEvent.setup();
    server.use(
      http.post("/v1/knowledge/datasets/:dataset_id/documents", () =>
        HttpResponse.json(
          envelope(
            "embedding_unavailable",
            "document ingest failed: ModelConnectionError: dead endpoint",
          ),
          { status: 502 },
        ),
      ),
    );
    renderWithProviders(<KnowledgeDatasetDetail />, {
      path: "/knowledge/:datasetId",
      initialEntries: ["/knowledge/kb-ds-1"],
    });
    await user.type(await screen.findByTestId("document-name-input"), "doomed");
    await user.type(screen.getByTestId("document-content-input"), "content");
    await user.click(screen.getByRole("button", { name: "Ingest document" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("dead endpoint");
  });

  it("shows the API error when the dataset is foreign/absent", async () => {
    server.use(
      http.get("/v1/knowledge/datasets/:dataset_id", () =>
        HttpResponse.json(
          envelope("not_found", "knowledge dataset 'nope' not found"),
          { status: 404 },
        ),
      ),
    );
    renderWithProviders(<KnowledgeDatasetDetail />, {
      path: "/knowledge/:datasetId",
      initialEntries: ["/knowledge/nope"],
    });
    expect(await screen.findByText(/not found/)).toBeInTheDocument();
  });
});

describe("<KnowledgeDatasetDetail/> file ingest", () => {
  it("reads a .md file into the content box client-side", async () => {
    const user = userEvent.setup();
    const file = new File(["# readme body"], "readme.md", { type: "text/markdown" });
    renderWithProviders(<KnowledgeDatasetDetail />, {
      path: "/knowledge/:datasetId",
      initialEntries: ["/knowledge/kb-ds-1"],
    });
    const input = await screen.findByTestId("document-file-input");
    await user.upload(input, file);
    await waitFor(() =>
      expect(screen.getByTestId("document-content-input")).toHaveValue("# readme body"),
    );
    // the file name seeds the name input
    expect(screen.getByTestId("document-name-input")).toHaveValue("readme");
  });
});
