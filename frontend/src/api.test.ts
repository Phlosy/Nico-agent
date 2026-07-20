import { afterEach, describe, expect, it, vi } from "vitest";

import { fetchReadiness } from "./api";

function response(payload: unknown): Response {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

const validPayload = {
  status: "ready",
  checked_at: "2026-07-16T15:00:00Z",
  components: {
    postgres: { status: "up", latency_ms: 2.4, detail: null },
  },
};

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("fetchReadiness", () => {
  it("rejects arrays at the components object boundary", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({ ...validPayload, components: [] })));

    await expect(fetchReadiness()).rejects.toThrow("无法识别的数据");
  });

  it("rejects timestamps that cannot be rendered", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(response({ ...validPayload, checked_at: "not-a-date" })),
    );

    await expect(fetchReadiness()).rejects.toThrow("无法识别的数据");
  });

  it("rejects invalid latency values", async () => {
    const components = {
      postgres: { status: "up", latency_ms: -1, detail: null },
    };
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(response({ ...validPayload, components })),
    );

    await expect(fetchReadiness()).rejects.toThrow("无法识别的数据");
  });
});
