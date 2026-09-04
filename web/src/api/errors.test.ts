import { describe, expect, it } from "vitest";

import { ApiError, apiErrorFromResponse, fieldErrors } from "./errors";

describe("ApiError", () => {
  it("carries status, kind, message, and details", () => {
    const error = new ApiError(409, "conflict", "name already exists", {
      foo: 1,
    });
    expect(error.status).toBe(409);
    expect(error.kind).toBe("conflict");
    expect(error.message).toBe("name already exists");
    expect(error.details).toEqual({ foo: 1 });
  });
});

describe("apiErrorFromResponse", () => {
  it("parses the frozen error envelope", () => {
    const response = Response.json(
      { error: { kind: "not_found", message: "agent 42 not found" } },
      { status: 404 },
    );
    const error = apiErrorFromResponse(response, {
      error: { kind: "not_found", message: "agent 42 not found" },
    });
    expect(error.status).toBe(404);
    expect(error.kind).toBe("not_found");
    expect(error.message).toBe("agent 42 not found");
  });

  it("falls back to a generic message on a non-envelope body", () => {
    const response = Response.json(null, { status: 500 });
    const error = apiErrorFromResponse(response, null);
    expect(error.status).toBe(500);
    expect(error.kind).toBe("internal");
    expect(error.message).toMatch(/status 500/);
  });
});

describe("fieldErrors", () => {
  it("maps 422 details.errors loc → first message", () => {
    const error = new ApiError(422, "validation", "request validation failed", {
      errors: [
        { loc: ["body", "name"], msg: "must not be blank" },
        { loc: ["body", "max_iterations"], msg: "greater than or equal to 1" },
      ],
    });
    expect(fieldErrors(error)).toEqual({
      name: "must not be blank",
      max_iterations: "greater than or equal to 1",
    });
  });

  it("keeps only the first segment after body (drops nested indices)", () => {
    const error = new ApiError(422, "validation", "request validation failed", {
      errors: [
        { loc: ["body", "tools", 0, "name"], msg: "tool name required" },
      ],
    });
    expect(fieldErrors(error)).toEqual({ tools: "tool name required" });
  });

  it("returns {} for non-422 errors and missing details", () => {
    expect(fieldErrors(new ApiError(409, "conflict", "dup"))).toEqual({});
    expect(fieldErrors(new ApiError(422, "validation", "no details"))).toEqual(
      {},
    );
    expect(fieldErrors(new Error("unrelated"))).toEqual({});
  });
});