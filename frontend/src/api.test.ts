import { describe, expect, it } from "vitest";
import { getHealth } from "./api";

function fakeFetch(status: number, body: unknown): typeof fetch {
  return (async () => new Response(JSON.stringify(body), { status })) as typeof fetch;
}

describe("getHealth", () => {
  it("returns the parsed health payload", async () => {
    const health = await getHealth(fakeFetch(200, { status: "ok", version: "0.1.0", api: "v1" }));
    expect(health.status).toBe("ok");
  });

  it("throws on a non-2xx response", async () => {
    await expect(getHealth(fakeFetch(503, {}))).rejects.toThrow("HTTP 503");
  });
});
