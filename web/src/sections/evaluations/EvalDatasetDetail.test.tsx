import { http, HttpResponse } from "msw";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { envelope, evalDatasetFixture } from "@/test/handlers";
import { server } from "@/test/msw";
import { renderWithProviders } from "@/test/render";

import { EvalDatasetDetail } from "./EvalDatasetDetail";

// A dataset whose judge_model carries the fields the editor panel doesn't
// collect (base_url, credential_ref — curl-only creation for now, S11 live
// session). The wholesale PATCH must never silently drop them.
const judgedFixture = {
  ...evalDatasetFixture,
  id: "ds-judge",
  name: "judge-vs-exact",
  scorers: [{ name: "llm_judge" as const }],
  judge_model: {
    provider: "openai_compatible",
    model: "gemma4:31b",
    base_url: "https://ollama.com/v1",
    credential_ref: { type: "env" as const, env_var: "OLLAMA_API_KEY" },
  },
};

function renderJudgedDataset() {
  const bodies: unknown[] = [];
  server.use(
    http.get("/v1/evaluations/datasets/ds-judge", () => HttpResponse.json(judgedFixture)),
    http.patch("/v1/evaluations/datasets/ds-judge", async ({ request }) => {
      const body = await request.json();
      bodies.push(body);
      return HttpResponse.json({ ...judgedFixture, ...(body as object) });
    }),
  );
  renderWithProviders(<EvalDatasetDetail />, {
    path: "/evaluations/datasets/:datasetId",
    initialEntries: ["/evaluations/datasets/ds-judge"],
  });
  return bodies;
}

describe("<EvalDatasetDetail/>", () => {
  it("preserves uncollected judge_model fields through a wholesale PATCH", async () => {
    const user = userEvent.setup();
    const bodies = renderJudgedDataset();

    await screen.findByDisplayValue("judge-vs-exact");
    expect(screen.getByText(/https:\/\/ollama\.com\/v1/)).toBeInTheDocument();

    await user.type(screen.getByLabelText("Name"), " v2");
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    await waitFor(() => expect(bodies).toHaveLength(1));
    expect(bodies[0]).toMatchObject({
      name: "judge-vs-exact v2",
      judge_model: {
        provider: "openai_compatible",
        model: "gemma4:31b",
        base_url: "https://ollama.com/v1",
        credential_ref: { type: "env", env_var: "OLLAMA_API_KEY" },
      },
    });
  });

  it("sends judge_model: null when the judge model is cleared", async () => {
    const user = userEvent.setup();
    const bodies = renderJudgedDataset();

    await screen.findByDisplayValue("judge-vs-exact");
    await user.click(screen.getByRole("button", { name: "Clear" }));
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    await waitFor(() => expect(bodies).toHaveLength(1));
    expect(bodies[0]).toMatchObject({ judge_model: null });
  });

  it("relays a 422 save error verbatim (llm_judge without judge model)", async () => {
    const user = userEvent.setup();
    server.use(
      http.get("/v1/evaluations/datasets/ds-judge", () => HttpResponse.json(judgedFixture)),
      http.patch("/v1/evaluations/datasets/ds-judge", () =>
        HttpResponse.json(
          envelope("validation", "scorer 'llm_judge' requires judge_model on the dataset"),
          { status: 422 },
        ),
      ),
    );
    renderWithProviders(<EvalDatasetDetail />, {
      path: "/evaluations/datasets/:datasetId",
      initialEntries: ["/evaluations/datasets/ds-judge"],
    });

    await screen.findByDisplayValue("judge-vs-exact");
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    expect(
      await screen.findByText("scorer 'llm_judge' requires judge_model on the dataset"),
    ).toBeInTheDocument();
  });
});