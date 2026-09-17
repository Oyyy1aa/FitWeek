import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import App from "./App";

const response = (body: unknown, ok = true) =>
  Promise.resolve({ ok, json: () => Promise.resolve(body) });

describe("FitWeek 中文首页", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("读取本地用户与本周计划，而不是展示静态占位数据", async () => {
    const fetch = vi
      .fn()
      .mockReturnValueOnce(
        response({
          id: "user-1",
          display_name: "小周",
          email: "local@example.test",
          timezone: "Asia/Shanghai",
          status: "active",
        }),
      )
      .mockReturnValueOnce(
        response([
          {
            id: "plan-1",
            week_start: "2026-07-27",
            status: "confirmed",
            estimated_total_minutes: 180,
            sessions: [{ id: "session-1" }, { id: "session-2" }],
          },
        ]),
      );
    vi.stubGlobal("fetch", fetch);

    render(<App />);

    expect(screen.getByText("正在同步训练数据…")).toBeInTheDocument();
    expect(await screen.findByText("小周，本周训练计划")).toBeInTheDocument();
    expect(screen.getByText("180 分钟")).toBeInTheDocument();
    expect(screen.getByText("2 次训练")).toBeInTheDocument();
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
    expect(fetch.mock.calls.map(([url]) => url)).toEqual([
      "/api/v1/users/me",
      "/api/v1/plans",
    ]);
  });

  it("在 API 不可用时明确提示降级状态", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline")));

    render(<App />);

    expect(await screen.findByText("训练数据暂时不可用")).toBeInTheDocument();
    expect(
      screen.getByText("请确认 FitWeek API 已启动；页面不会把失败伪装成成功。"),
    ).toBeInTheDocument();
  });
});
