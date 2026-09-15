import { http, HttpResponse } from "msw";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { envelope, evalDatasetFixture } from "@/test/handlers";
import { server } from "@/test/msw";
import { renderWithProviders } from "@/test/render";

import { EvaluationsList } from "./EvaluationsList";

describe("<EvaluationsList/>", () => {
  it("renders datasets from the API payload", async () => {
    renderWithProviders(<EvaluationsList />);
    expect(await screen.findByRole("link", { name: "smoke-dataset" })).toBeInTheDocument();
    expect(screen.getByText("exact")).toBeInTheDocument();
  });

  it("shows an honest empty state", async () => {
    server.use(
      http.get("/v1/evaluations/datasets", () => HttpResponse.json({ items: [] })),
    );
    renderWithProviders(<EvaluationsList />);
    expect(await screen.findByText(/No datasets yet/)).toBeInTheDocument();
  });

  it("shows the API error message on failure", async () => {
    server.use(
      http.get("/v1/evaluations/datasets", () =>
        HttpResponse.json(envelope("internal", "database unavailable"), { status: 500 }),
      ),
    );
    renderWithProviders(<EvaluationsList />);
    expect(await screen.findByText("database unavailable")).toBeInTheDocument();
  });

  it("creates a dataset with the first case input + exact scorer", async () => {
    const user = userEvent.setup();
    const bodies: unknown[] = [];
    server.use(
      http.post("/v1/evaluations/datasets", async ({ request }) => {
        bodies.push(await request.json());
        return HttpResponse.json({ ...evalDatasetFixture, id: "ds-2" }, { status: 201 });
      }),
    );

    renderWithProviders(<EvaluationsList />);
    await user.click(await screen.findByRole("button", { name: "New dataset" }));
    await user.type(await screen.findByLabelText("Dataset name"), "regression set");
    // an empty first-case input is a 422 at the backend boundary — the form
    // collects it here and the submit stays disabled until it's non-empty
    await user.type(await screen.findByLabelText("First case input"), "what is 2+2?");
    await user.click(screen.getByRole("button", { name: "Create dataset" }));

    await waitFor(() => expect(bodies).toHaveLength(1));
    expect(bodies[0]).toEqual({
      name: "regression set",
      description: "",
      cases: [{ id: "c-1", input: "what is 2+2?", expected: "" }],
      scorers: [{ name: "exact" }],
    });
  });

  it("keeps create disabled until the first case input is filled", async () => {
    const user = userEvent.setup();
    const createSpy = vi.fn(() => HttpResponse.json(evalDatasetFixture, { status: 201 }));
    server.use(http.post("/v1/evaluations/datasets", () => createSpy()));

    renderWithProviders(<EvaluationsList />);
    await user.click(await screen.findByRole("button", { name: "New dataset" }));
    await user.type(await screen.findByLabelText("Dataset name"), "no input yet");
    expect(screen.getByRole("button", { name: "Create dataset" })).toBeDisabled();
    expect(createSpy).not.toHaveBeenCalled();
  });

  it("cancels the create form without POSTing", async () => {
    const user = userEvent.setup();
    const createSpy = vi.fn(() => HttpResponse.json(evalDatasetFixture, { status: 201 }));
    server.use(http.post("/v1/evaluations/datasets", () => createSpy()));

    renderWithProviders(<EvaluationsList />);
    await user.click(await screen.findByRole("button", { name: "New dataset" }));
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByLabelText("Dataset name")).not.toBeInTheDocument();
    expect(createSpy).not.toHaveBeenCalled();
  });

  it("toasts the server message verbatim when delete conflicts (409)", async () => {
    const user = userEvent.setup();
    server.use(
      http.delete("/v1/evaluations/datasets/:dataset_id", () =>
        HttpResponse.json(
          envelope("conflict", "dataset has eval runs; delete them first"),
          { status: 409 },
        ),
      ),
    );
    vi.spyOn(window, "confirm").mockReturnValue(true);

    renderWithProviders(<EvaluationsList />);
    await user.click(await screen.findByRole("button", { name: "Delete" }));
    expect(
      await screen.findByText("dataset has eval runs; delete them first"),
    ).toBeInTheDocument();
  });

  it("does not delete when confirmation is dismissed", async () => {
    const user = userEvent.setup();
    const deleteSpy = vi.fn(() => new HttpResponse(null, { status: 204 }));
    server.use(http.delete("/v1/evaluations/datasets/:dataset_id", () => deleteSpy()));
    vi.spyOn(window, "confirm").mockReturnValue(false);

    renderWithProviders(<EvaluationsList />);
    await user.click(await screen.findByRole("button", { name: "Delete" }));
    expect(deleteSpy).not.toHaveBeenCalled();
  });
});