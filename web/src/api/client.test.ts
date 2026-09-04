import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import { server } from "@/test/msw";
import { envelope } from "@/test/handlers";

import { client, unwrap } from "./client";
import { ApiError } from "./errors";

describe("unwrap", () => {
  it("returns the parsed body on 2xx (real generated client over msw)", async () => {
    const payload = await unwrap(client.GET("/v1/capabilities"));
    expect(payload.sections.agents?.enabled).toBe(true);
  });

  it("throws the envelope ApiError on 4xx/5xx", async () => {
    server.use(
      http.get("/v1/capabilities", () =>
        HttpResponse.json(envelope("conflict", "duplicate agent name"), {
          status: 409,
        }),
      ),
    );
    const call = unwrap(client.GET("/v1/capabilities"));
    await expect(call).rejects.toMatchObject({
      name: "ApiError",
      status: 409,
      kind: "conflict",
      message: "duplicate agent name",
    });
  });

  it("survives a non-JSON error body", async () => {
    server.use(
      http.get("/v1/capabilities", () => new HttpResponse("boom", { status: 502 })),
    );
    const error = await unwrap(client.GET("/v1/capabilities")).then(
      () => null,
      (e: unknown) => e,
    );
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(502);
  });
});