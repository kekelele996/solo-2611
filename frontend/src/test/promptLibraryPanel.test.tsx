import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../api/client";
import type { PromptLibraryResponse } from "../api/types";
import { PromptLibraryPanel } from "../components/source/PromptLibraryPanel";
import { ToastProvider } from "../components/toast";

const LIBRARY: PromptLibraryResponse = {
  total: 2,
  items: [
    {
      recordId: "task:1",
      projectId: "project-a",
      engineeringId: "eng-1",
      engineeringName: "GB-544",
      taskId: "1",
      taskName: "订单对账",
      subDirection: "数仓与数据湖",
      taskType: "功能开发",
      text: "按来源时间合并订单，重复执行不能新增记录。",
      usedAt: "2026-11-13T10:00:00+08:00",
      source: "live",
    },
    {
      recordId: "task:2",
      projectId: "project-a",
      engineeringId: "eng-2",
      engineeringName: "GB-501",
      taskId: "2",
      taskName: "旧任务",
      subDirection: "数据分析与数据应用",
      taskType: "Bug 修复",
      text: "旧任务留下的提示词。",
      usedAt: "2026-11-12T10:00:00+08:00",
      source: "history",
    },
  ],
};

describe("PromptLibraryPanel", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api, "promptLibrary").mockResolvedValue(LIBRARY);
  });
  afterEach(cleanup);

  it("shows project prompts and supports JSONL import", async () => {
    const imported = vi.spyOn(api, "importPromptLibrary").mockResolvedValue({
      ok: true,
      projectId: "project-a",
      imported: 1,
      skipped: 0,
      errors: [],
      total: 3,
    });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { container } = render(
      <QueryClientProvider client={client}>
        <ToastProvider>
          <PromptLibraryPanel projectId="project-a" projectName="项目 A" />
        </ToastProvider>
      </QueryClientProvider>,
    );

    expect(await screen.findByText("GB-544")).toBeTruthy();
    expect(screen.getByText("订单对账")).toBeTruthy();
    expect(screen.getByText("按来源时间合并订单，重复执行不能新增记录。")).toBeTruthy();
    expect(screen.getByText(/2 个工程/)).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: /GB-501/ }));
    expect(screen.getByText("旧任务留下的提示词。")).toBeTruthy();

    fireEvent.change(screen.getByPlaceholderText("搜索工程、任务或提示词"), {
      target: { value: "旧任务" },
    });
    expect(screen.getByText("旧任务留下的提示词。")).toBeTruthy();
    expect(screen.queryByText("按来源时间合并订单，重复执行不能新增记录。")).toBeNull();

    const input = container.querySelector<HTMLInputElement>('input[type="file"]');
    if (!input) throw new Error("缺少导入文件输入");
    const file = new File(['{"text":"导入的新提示词。"}\n'], "prompts.jsonl", {
      type: "application/json",
    });
    fireEvent.change(input, { target: { files: [file] } });
    await waitFor(() =>
      expect(imported).toHaveBeenCalledWith(
        "project-a",
        [expect.objectContaining({ text: "导入的新提示词。" })],
        "prompts.jsonl",
      ),
    );
  });
});
