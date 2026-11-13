import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../api/client";
import type { CoreTraceResponse } from "../api/types";
import { CoreTracePanel } from "../components/task/CoreTracePanel";

const TRACE: CoreTraceResponse = {
  steps: [
    {
      no: 1,
      kind: "command",
      tool: "Bash",
      summary: "pytest -q",
      note: "运行测试",
      target: "",
      status: "ok",
      ts: "2026-11-13T14:00:00Z",
      duration_ms: 1250,
      input: { command: "pytest -q" },
      output: "3 passed",
      output_chars: 8,
      tool_use_id: "tool-1",
    },
    {
      no: 2,
      kind: "edit",
      tool: "Edit",
      summary: "/workspace/backend/app.py",
      note: "修改文件内容",
      target: "/workspace/backend/app.py",
      status: "ok",
      ts: "2026-11-13T14:00:02Z",
      duration_ms: 320,
      input: { file_path: "/workspace/backend/app.py" },
      output: "updated",
      output_chars: 7,
      tool_use_id: "tool-2",
    },
  ],
  counts: {
    total: 2,
    failed: 0,
    running: 0,
    commands: 1,
    edits: 1,
    reads: 0,
    searches: 0,
    by_kind: {
      command: 1,
      edit: 1,
      read: 0,
      search: 0,
      agent: 0,
      network: 0,
      other: 0,
    },
  },
  modified_files: ["/workspace/backend/app.py"],
  read_files: [],
  source: {
    path: "/tasks/example/monitor/runs/attempt-01/live/session.jsonl",
    session_id: "session-1",
    models: ["auto_model/urm"],
    updated_at: "2026-11-13T14:00:03Z",
    live: true,
    mode: "live",
  },
};

describe("CoreTracePanel", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api, "coreTrace").mockResolvedValue(TRACE);
  });

  afterEach(cleanup);

  it("visualizes only Claude Code tool execution", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <CoreTracePanel taskId="task-1" />
      </QueryClientProvider>,
    );

    expect(await screen.findByText("pytest -q")).toBeInTheDocument();
    expect(screen.getByText("Claude Code 执行轨迹")).toBeInTheDocument();
    expect(screen.getByText("/workspace/backend/app.py")).toBeInTheDocument();
    expect(screen.getByText("实时写入")).toBeInTheDocument();
    expect(screen.getByText("2 个动作")).toBeInTheDocument();
  });

  it("expands command input and output and filters action kinds", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <CoreTracePanel taskId="task-1" />
      </QueryClientProvider>,
    );

    fireEvent.click(await screen.findByText("pytest -q"));
    expect(await screen.findByText("3 passed")).toBeInTheDocument();
    expect(screen.getByText(/"command": "pytest -q"/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "命令 1" }));
    await waitFor(() => expect(screen.queryByText("pytest -q")).not.toBeInTheDocument());
  });
});
