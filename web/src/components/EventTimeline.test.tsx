import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { TimelineItem } from "@/stores/runConsole";

import { EventTimeline } from "./EventTimeline";

describe("<EventTimeline/>", () => {
  it("renders a failed tool card with its interpolated error kind", () => {
    // Regression: the kind used to render literally as "[{item.errorKind}]"
    // — a template literal without ${} interpolation inside the JSX text.
    const items: TimelineItem[] = [
      {
        kind: "tool",
        id: "t1",
        toolCallId: "c1",
        name: "http_get",
        status: "failed",
        arguments: { url: "http://bkharshit.com" },
        error: "ValueError: host 'bkharshit.com' is not in the allow-list []",
        errorKind: "validation",
      },
    ];
    render(<EventTimeline items={items} />);

    expect(screen.getByTestId("tool-error-c1").textContent).toBe(
      "[validation] ValueError: host 'bkharshit.com' is not in the allow-list []",
    );
  });

  it("renders a declined tool card as declined (ADR 0011 §3)", () => {
    const items: TimelineItem[] = [
      {
        kind: "tool",
        id: "t2",
        toolCallId: "c2",
        name: "current_time",
        status: "declined",
        arguments: {},
      },
    ];
    render(<EventTimeline items={items} />);

    expect(screen.getByText("declined")).toBeInTheDocument();
  });
});