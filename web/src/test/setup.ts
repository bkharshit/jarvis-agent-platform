import "@testing-library/jest-dom/vitest";

import { cleanup } from "@testing-library/react";
import { afterEach, beforeAll, afterAll } from "vitest";

import { server } from "./msw";

// The app client uses same-origin relative paths (the Vite proxy seam), but
// undici's `Request` (which openapi-fetch constructs before calling fetch)
// rejects them outside a browser. Rewrite relative to a test origin; msw
// matches handlers by pathname regardless of origin.
const RealRequest = globalThis.Request;
class TestRequest extends RealRequest {
  constructor(input: string | Request | URL, init?: RequestInit) {
    super(
      typeof input === "string" && input.startsWith("/")
        ? `http://localhost${input}`
        : input,
      init,
    );
  }
}
// undici's Request is not constructible as a plain superclass (its brand
// check requires the real prototype); a forward function keeps TS happy.
globalThis.Request = TestRequest as typeof Request;

// jsdom has no ResizeObserver; the workflow canvas (@xyflow/react) needs one.
class ResizeObserverStub {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}
globalThis.ResizeObserver ??= ResizeObserverStub as unknown as typeof ResizeObserver;

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterAll(() => server.close());

afterEach(() => {
  cleanup();
  server.resetHandlers();
});