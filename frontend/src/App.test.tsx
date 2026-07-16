import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import { App } from "./App";

const readyPayload = {
  status: "ready",
  checked_at: "2026-07-16T15:00:00Z",
  components: {
    postgres: { status: "up", latency_ms: 2.4, detail: null },
    redis: { status: "up", latency_ms: 0.8, detail: null },
    minio: { status: "up", latency_ms: 4.1, detail: null },
  },
};

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("infrastructure status page", () => {
  it("shows an honest loading state while the API is pending", () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>(() => undefined)));

    render(<App />, { wrapper });

    expect(screen.getByText("正在连接基础设施")).toBeInTheDocument();
  });

  it("renders all real components when the API reports ready", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(readyPayload)));

    render(<App />, { wrapper });

    expect(await screen.findByText("平台基础设施已就绪")).toBeInTheDocument();
    expect(screen.getByText("PostgreSQL + pgvector")).toBeInTheDocument();
    expect(screen.getByText("Redis")).toBeInTheDocument();
    expect(screen.getByText("MinIO")).toBeInTheDocument();
    expect(screen.getAllByText("正常")).toHaveLength(3);
  });

  it("keeps the component details returned with HTTP 503", async () => {
    const degraded = {
      ...readyPayload,
      status: "not_ready",
      components: {
        ...readyPayload.components,
        redis: { status: "down", latency_ms: 20, detail: "ConnectionError: offline" },
      },
    };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(degraded, 503)));

    render(<App />, { wrapper });

    expect(await screen.findByText("基础设施需要关注")).toBeInTheDocument();
    expect(screen.getByText("ConnectionError: offline")).toBeInTheDocument();
    expect(screen.getByText("异常")).toBeInTheDocument();
  });

  it("reports transport failures and supports a manual retry", async () => {
    const fetchMock = vi
      .fn()
      .mockRejectedValueOnce(new TypeError("network unavailable"))
      .mockResolvedValueOnce(jsonResponse(readyPayload));
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    render(<App />, { wrapper });

    expect(await screen.findByText("无法读取平台状态")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "重新检查" }));

    expect(await screen.findByText("平台基础设施已就绪")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});

