import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { RunInspector } from "./run-inspector";

const TENANT_ID = "11111111-1111-4111-8111-111111111111";
const RUN_ID = "22222222-2222-4222-8222-222222222222";

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

function response(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function routeFetch(overrides: Record<string, unknown> = {}) {
  return vi.fn((input: RequestInfo | URL) => {
    const path = new URL(String(input), "http://test").pathname;
    const suffix = path.replace(`/api/v1/runs/${RUN_ID}`, "") || "/run";
    const defaults: Record<string, unknown> = {
      "/run": {
        id: RUN_ID,
        status: "completed",
        result: { content: "done" },
        budgets: {},
        cost: {},
      },
      "/runtime": {
        provider_name: "nico_native",
        protocol_version: "2.0",
        execution_mode: "direct",
      },
      "/steps": [],
      "/model-calls": [],
      "/plans": [],
      "/children": [],
      "/delegations": [],
      "/messages": [],
      "/artifacts": [],
      "/events": [],
    };
    return Promise.resolve(response(overrides[suffix] ?? defaults[suffix] ?? []));
  });
}

afterEach(() => {
  window.history.replaceState(null, "", "/");
  vi.unstubAllGlobals();
});

describe("read-only Run Inspector", () => {
  it("shows loading for a valid deep link", () => {
    window.history.replaceState(null, "", `/?tenant=${TENANT_ID}&run=${RUN_ID}`);
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>(() => undefined)));

    render(<RunInspector />, { wrapper });

    expect(screen.getByText("正在读取运行事实…")).toBeInTheDocument();
  });

  it("renders fixed section order and honest empty states", async () => {
    window.history.replaceState(null, "", `/?tenant=${TENANT_ID}&run=${RUN_ID}`);
    vi.stubGlobal("fetch", routeFetch());

    render(<RunInspector />, { wrapper });

    expect(await screen.findByText("状态与结果")).toBeInTheDocument();
    expect(screen.getAllByRole("heading", { level: 3 }).map((item) => item.textContent)).toEqual([
      "状态与结果",
      "步骤与模型用量",
      "Plan",
      "Child tree",
      "消息与 Artifact",
      "预算与审计",
    ]);
    expect(screen.getByText("尚无执行步骤")).toBeInTheDocument();
    expect(screen.getByText("此运行没有 Plan")).toBeInTheDocument();
    expect(screen.getByText("没有 Child Run")).toBeInTheDocument();
    expect(screen.getByText("没有 Artifact")).toBeInTheDocument();
  });

  it("marks cancelled and partial usage while redacting and escaping content", async () => {
    window.history.replaceState(null, "", `/?tenant=${TENANT_ID}&run=${RUN_ID}`);
    const malicious = '<img src=x onerror="alert(1)">';
    vi.stubGlobal(
      "fetch",
      routeFetch({
        "/run": {
          id: RUN_ID,
          status: "cancelled",
          result: { content: malicious, api_key: "must-not-render" },
          budgets: { token_budget: 100 },
          cost: {},
        },
        "/model-calls": [
          {
            id: "call-1",
            model: "provider-model",
            status: "completed",
            usage_status: "partial",
            cost_status: "estimated",
          },
        ],
      }),
    );

    const { container } = render(<RunInspector />, { wrapper });

    expect(await screen.findByText("该运行已取消。")).toBeInTheDocument();
    expect(screen.getByText("部分 Provider 用量或费用为 partial/estimated。")).toBeInTheDocument();
    const resultJson = container.querySelector(".safe-json");
    expect(resultJson).toHaveTextContent("<img src=x");
    expect(resultJson).toHaveTextContent("alert(1)");
    expect(resultJson?.querySelector("img")).toBeNull();
    expect(container).not.toHaveTextContent("must-not-render");
    expect(container).toHaveTextContent("[REDACTED]");
  });

  it("supports keyboard form submission and reports core API errors", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({}, 404)));
    const user = userEvent.setup();
    render(<RunInspector />, { wrapper });

    await user.type(screen.getByLabelText("Tenant ID"), TENANT_ID);
    await user.type(screen.getByLabelText("Run ID"), RUN_ID);
    await user.tab();
    expect(screen.getByRole("button", { name: "读取运行" })).toHaveFocus();
    await user.keyboard("{Enter}");

    expect(await screen.findByRole("alert")).toHaveTextContent("API 返回 HTTP 404");
    const inspector = screen.getByRole("region", { name: "运行检查器" });
    expect(within(inspector).getByLabelText("选择要检查的运行")).toBeInTheDocument();
  });
});
