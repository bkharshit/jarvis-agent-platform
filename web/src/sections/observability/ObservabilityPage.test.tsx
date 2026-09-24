import { http, HttpResponse } from "msw";
import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { envelope, traceSummaryFixture } from "@/test/handlers";
import { server } from "@/test/msw";
import { renderWithProviders } from "@/test/render";

import { ObservabilityPage } from "./ObservabilityPage";

describe("<ObservabilityPage/>", () => {
  it("renders the trace list with run/span/kind/status columns", async () => {
    renderWithProviders(<ObservabilityPage />);
    const link = await screen.findByRole("link", { name: "b2f1c0d4…" });
    expect(link).toHaveAttribute("href", `/observability/${traceSummaryFixture.trace_id}`);
    expect(screen.getByText("1")).toBeInTheDocument(); // run_ids.length
    expect(screen.getByText("4")).toBeInTheDocument(); // span_count
    expect(screen.getByText("iteration, llm.call, run.segment, tool.call")).toBeInTheDocument();
    expect(screen.getByText("ok")).toBeInTheDocument();
  });

  it("shows an honest empty state before the first run", async () => {
    server.use(http.get("/v1/traces", () => HttpResponse.json({ traces: [] })));
    renderWithProviders(<ObservabilityPage />);
    expect(
      await screen.findByText("No traces yet. Run an agent to produce spans."),
    ).toBeInTheDocument();
  });

  it("renders the backend error when the route 503s (observability disabled)", async () => {
    server.use(
      http.get("/v1/traces", () =>
        HttpResponse.json(
          envelope("observability_disabled", "Observability is disabled on this deployment."),
          { status: 503 },
        ),
      ),
    );
    renderWithProviders(<ObservabilityPage />);
    expect(await screen.findByText(/Observability is disabled/)).toBeInTheDocument();
  });
});