import { http, HttpResponse } from "msw";
import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { envelope, spansFixture } from "@/test/handlers";
import { server } from "@/test/msw";
import { renderWithProviders } from "@/test/render";

import { TraceDetailPage } from "./TraceDetail";

describe("<TraceDetailPage/>", () => {
  it("renders every span with kind labels, offsets, and durations", async () => {
    renderWithProviders(<TraceDetailPage />, {
      path: "/observability/:traceId",
      initialEntries: ["/observability/b2f1c0d4a596778899aabbccddeeff00"],
    });
    // Segment row (depth 0).
    expect(await screen.findByText("run.segment")).toBeInTheDocument();
    // Children under it (the "iteration" text also appears as its kind
    // label, so match all occurrences).
    expect(screen.getAllByText("iteration").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("llm.call")).toBeInTheDocument();
    expect(screen.getByText("calculator")).toBeInTheDocument();
    // Duration formatting: seconds at ≥1s, ms below.
    expect(screen.getByText("2.00s")).toBeInTheDocument();
    expect(screen.getByText("900ms")).toBeInTheDocument();
    expect(screen.getByText("30ms")).toBeInTheDocument();
    // Offset of the llm.call from the segment's start.
    expect(screen.getByText("150ms")).toBeInTheDocument();
  });

  it("indents children under their parent (waterfall depth)", async () => {
    renderWithProviders(<TraceDetailPage />, {
      path: "/observability/:traceId",
      initialEntries: ["/observability/b2f1c0d4a596778899aabbccddeeff00"],
    });
    await screen.findByText("run.segment");
    const depthOf = (name: string) => {
      // First match is the span-name cell (the kind label renders after it).
      const row = screen.getAllByText(name)[0].closest("div");
      return row ? row.style.paddingLeft : "";
    };
    expect(depthOf("run.segment")).toBe("0px");
    expect(depthOf("iteration")).toBe("16px");
    expect(depthOf("llm.call")).toBe("32px");
  });

  it("colors an errored span red with its status message", async () => {
    server.use(
      http.get("/v1/traces/:trace_id", () =>
        HttpResponse.json({
          trace_id: "b2f1c0d4a596778899aabbccddeeff00",
          spans: [
            { ...spansFixture[0], status: "error", status_message: "strategy 'function_calling' raised ConnectError" },
            ...spansFixture.slice(1),
          ],
        }),
      ),
    );
    renderWithProviders(<TraceDetailPage />, {
      path: "/observability/:traceId",
      initialEntries: ["/observability/b2f1c0d4a596778899aabbccddeeff00"],
    });
    const badge = await screen.findByText("error");
    expect(badge).toHaveClass("text-red-400");
    expect(screen.getByText(/ConnectError/)).toBeInTheDocument();
  });

  it("colors a cancelled span amber", async () => {
    server.use(
      http.get("/v1/traces/:trace_id", () =>
        HttpResponse.json({
          trace_id: "b2f1c0d4a596778899aabbccddeeff00",
          spans: [
            { ...spansFixture[0], status: "cancelled", status_message: "cancelled by user" },
            ...spansFixture.slice(1),
          ],
        }),
      ),
    );
    renderWithProviders(<TraceDetailPage />, {
      path: "/observability/:traceId",
      initialEntries: ["/observability/b2f1c0d4a596778899aabbccddeeff00"],
    });
    expect(await screen.findByText("cancelled")).toHaveClass("text-amber-400");
  });

  it("lands on the back link with the backend's 404 for an unknown trace", async () => {
    renderWithProviders(<TraceDetailPage />, {
      path: "/observability/:traceId",
      initialEntries: ["/observability/00000000000000000000000000000000"],
    });
    expect(await screen.findByText(/trace .* not found/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Back to traces" })).toHaveAttribute(
      "href",
      "/observability",
    );
  });

  it("renders the disabled-observability 404 envelope as an error with a way back", async () => {
    server.use(
      http.get("/v1/traces/:trace_id", () =>
        HttpResponse.json(
          envelope("observability_disabled", "Observability is disabled on this deployment."),
          { status: 503 },
        ),
      ),
    );
    renderWithProviders(<TraceDetailPage />, {
      path: "/observability/:traceId",
      initialEntries: ["/observability/b2f1c0d4a596778899aabbccddeeff00"],
    });
    expect(await screen.findByText(/Observability is disabled/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Back to traces" })).toBeInTheDocument();
  });
});