import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../api/client";
import type { ConfigResponse } from "../api/types";
import { ToastProvider } from "../components/toast";
import { SystemPage } from "../pages/SystemPage";

const CONFIG: ConfigResponse = {
  host: "127.0.0.1",
  port: 8099,
  skill_root: "/skills/solo-2611",
  skill_version: "0.2.3",
  tasks_dir: "/tasks",
  source: {
    base_url: "http://hub",
    auth_mode: "cookie",
    username: "u",
    timeout_seconds: 20,
    password: "<set>",
    bearer_token: "<unset>",
  },
  execution: { max_containers: 4, run_timeout_seconds: 7200, attempts: 3, stale_after_seconds: 5400 },
  storage: { disk_floor_gb: 30, event_retention_days: 30, skill_log_retention_days: 14 },
  recording: { max_seconds: 90, chrome_url: "http://127.0.0.1:3000" },
  autopilot: {
    enabled: true,
    agent_bin: "",
    agent_args: [],
    restricted: false,
    stage_timeout_seconds: 3600,
    stage_attempts: 2,
    run_attempts: 2,
    parallel_authoring: 2,
    parallel_delivering: 1,
    stop_kills_containers: true,
    breaker: { same_kind_failures: 3, audit_reject_rate: 0.2, audit_reject_window: 10 },
  },
  audit: { mode: "sample", sample_rate: 0.3, stratify_by: "task_type" },
  intake: { max_in_hand: 0, exclude_tags: [] },
  trash: { retention_days: 7 },
  slim: { enabled: false, after_days: 3, patterns: ["node_modules", ".venv"] },
  notify: { macos: true, webhook_url: "" },
  skill_config: {},
  github_token: "<set>",
};

function field(label: string): HTMLInputElement {
  const node = screen.getByText(label).closest("label")?.querySelector("input");
  if (!node) throw new Error(`找不到字段：${label}`);
  return node;
}

describe("settings page", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api, "config").mockResolvedValue(CONFIG);
    vi.spyOn(api, "logs").mockResolvedValue({ items: [] });
    vi.spyOn(api, "systemStatus").mockResolvedValue({} as Awaited<ReturnType<typeof api.systemStatus>>);
    vi.spyOn(api, "trash").mockResolvedValue({ items: [], total: 0, total_bytes: 0, retention_days: 7 });
  });

  afterEach(cleanup);

  // 每个可编辑字段改动后都必须能保存，并且不会被后台刷新冲掉
  it.each([
    ["脚本目录", "/skills/other", (body: Record<string, unknown>) => body.skill_root],
    ["容器并发", "2", (body: Record<string, { [key: string]: unknown }>) => body.execution.max_containers],
    ["超时秒数", "3600", (body: Record<string, { [key: string]: unknown }>) => body.execution.run_timeout_seconds],
    ["尝试次数", "5", (body: Record<string, { [key: string]: unknown }>) => body.execution.attempts],
    ["任务目录", "/other", (body: Record<string, unknown>) => body.tasks_dir],
    ["磁盘阈值 GB", "10", (body: Record<string, { [key: string]: unknown }>) => body.storage.disk_floor_gb],
    ["Chrome URL", "http://127.0.0.1:5000", (body: Record<string, { [key: string]: unknown }>) => body.recording.chrome_url],
  ])("saves an edit to %s", async (label, value, pick) => {
    const patch = vi.spyOn(api, "patchConfig").mockResolvedValue(CONFIG);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <ToastProvider>
          <SystemPage />
        </ToastProvider>
      </QueryClientProvider>,
    );
    const save = await screen.findByRole("button", { name: "配置已保存" });
    expect(save).toBeDisabled();

    fireEvent.change(field(label), { target: { value } });
    await client.invalidateQueries();
    expect(field(label).value).toBe(value);

    const button = screen.getByRole("button", { name: "保存配置" });
    expect(button).toBeEnabled();
    fireEvent.click(button);
    await waitFor(() => expect(patch).toHaveBeenCalledTimes(1));
    expect(String(pick(patch.mock.calls[0][0] as never))).toBe(value);
    await screen.findByRole("button", { name: "配置已保存" });
  });
});
