import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../api/client";
import type { DispatchSnapshot } from "../api/types";
import { ToastProvider } from "../components/toast";
import { DispatchPage } from "../pages/DispatchPage";

const SNAPSHOT: DispatchSnapshot = {
  engine: "v3",
  enabled: true,
  desired_state: "running",
  effective_state: "held",
  sentence: "已自动挂起 · 容器 2/4 · 会话 1/5 · 在手 3/5 · 网关断开，02:30 复测",
  started_at: "2026-11-11T18:00:00+08:00",
  uptime_seconds: 7200,
  settings: { containers: 4, max_tasks: 5 },
  limits: {
    mode: "containers",
    containers: 4,
    max_tasks: 5,
    create_cooldown_seconds: 180,
    recording: 1,
    skill_limit: 4,
    synced: true,
  },
  creation: {
    allowed: false,
    code: "create.cooldown",
    text: "建题冷却中，还需 120 秒",
    until: "2099-01-01T00:02:00+08:00",
    last_created_at: "2099-01-01T00:00:00+08:00",
    next_allowed_at: "2099-01-01T00:02:00+08:00",
    cooldown_seconds: 180,
    in_hand: 3,
    max_tasks: 5,
    containers: { live: 2, waiting: 1, extra: 0, used: 3, limit: 4 },
  },
  pools: {
    containers: { name: "containers", used: 2, limit: 4, available: 2, draining: false },
    sessions: { name: "sessions", used: 1, limit: 5, available: 4, draining: false },
    authoring: { name: "authoring", used: 0, limit: 2, available: 2, draining: false },
    delivering: { name: "delivering", used: 0, limit: 1, available: 1, draining: false },
    recording: { name: "recording", used: 0, limit: 1, available: 1, draining: false },
    exporting: { name: "exporting", used: 0, limit: 1, available: 1, draining: false },
  },
  holds: [
    {
      id: "llm_guard",
      source: "llm_guard",
      scope: ["containers"],
      reason_code: "gateway.unreachable",
      reason: "网关连续 2 次探测失败",
      since: "2026-11-11T19:55:00+08:00",
      auto: true,
      next_at: "2026-11-11T20:05:00+08:00",
      count: 2,
      last_error: "timeout",
      self_heal_deadline: "",
      caused_by_task: "",
    },
  ],
  hold_history: [],
  counts: { active: 1, waiting: 1, needs_action: 1, finished: 2, all: 5 },
  queue: [
    {
      task_id: "task-1",
      task_name: "列级血缘增量分析",
      status: "quarantined",
      stage: "running",
      priority: 100,
      position: 0,
      enqueued_at: "2026-11-11T19:00:00+08:00",
      not_before: "",
      wait_code: "quarantined",
      wait_text: "门禁连续两次相同错误",
      wait_until: "",
      attempts: { running: 2 },
      total_attempts: 2,
      last_failure: { kind: "gate.rejected" },
      repeat_signature: "abc",
      guard_flags: [],
      keep: false,
      plan_id: "",
      project_id: "",
      engineering_id: "eng-1",
      updated_at: "2026-11-11T19:30:00+08:00",
      active: false,
      queue_position: 1,
      job_status: "blocked",
      failure_label: "gate.rejected",
      matched_by: "gate_errors",
      autopilot_detail: "ChatGPT 已打开但未收到首条消息，已自动补按发送（1/2）",
      desktop_running: true,
      desktop_recovery_attempts: 1,
      desktop_recovery_last_at: "2026-11-11T19:30:10+08:00",
      desktop_recovery_error: "",
      reason: "门禁连续两次返回相同错误",
      gate: {
        item_id: "task-1",
        stage: "running",
        allowed: false,
        code: "quarantined",
        text: "门禁连续两次返回相同错误",
        until: "",
        gate: "G12",
        checks: [{ gate: "G12", ok: false, code: "quarantined", text: "门禁连续两次返回相同错误", until: "" }],
      },
    },
  ],
  upcoming: [],
  guard: { mode: "observe", count: 0, hits: [] },
  throughput: {
    hours: 24,
    completed: 12,
    failed: 2,
    quarantined: 1,
    per_hour: 0.5,
    medians_seconds: { running_s: 1800 },
    container_utilization: 0.87,
  },
  capacity: { items: [] },
  plan: { mode: "target", target: 30 },
  loops: [],
  keep_awake: true,
};

describe("dispatch page", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api, "dispatch").mockResolvedValue(SNAPSHOT);
    vi.spyOn(api, "dispatchUpcoming").mockResolvedValue({ items: [] });
    vi.spyOn(api, "dispatchLimits").mockResolvedValue({
      snapshot: SNAPSHOT,
      changed: { containers: [4, 5] },
      sync: { requested: 5, actual: 5, synced: true, ceiling: 6 },
    });
    vi.spyOn(api, "dispatchQueueAction").mockResolvedValue({ ok: true, snapshot: SNAPSHOT });
    vi.spyOn(api, "dispatchProbeHold").mockResolvedValue({
      source: "llm_guard",
      ok: true,
      message: "网关可用",
      detail: {},
    });
    vi.spyOn(api, "dispatchProbe").mockResolvedValue({
      source: "llm_guard",
      ok: true,
      message: "网关可用",
      detail: {},
    });
    vi.spyOn(api, "dispatchRecover").mockResolvedValue({ requeued: 1, preserved: 0, task_ids: ["task-1"] });
  });

  afterEach(cleanup);

  it("renders the state sentence, scoped hold and queue reason", async () => {
    renderPage();
    expect(await screen.findByRole("heading", { name: "任务队列" })).toBeInTheDocument();
    expect(screen.getByText(SNAPSHOT.sentence)).toBeInTheDocument();
    expect(screen.getByText("网关连续 2 次探测失败")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /待处理/ })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /正在处理/ })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /已处理/ })).toBeInTheDocument();
    expect(screen.getByText("门禁连续两次返回相同错误")).toBeInTheDocument();
    expect(screen.getByText("自动补发 1/2")).toBeInTheDocument();
    fireEvent.click(screen.getByText("门禁连续两次返回相同错误"));
    expect(screen.getByText("WHY NOT RUNNING")).toBeInTheDocument();
    expect(screen.getByText("G12")).toBeInTheDocument();
  });

  it("offers no manual failure handling: failures requeue and holds self-heal", async () => {
    renderPage();
    expect(await screen.findByText("网关连续 2 次探测失败")).toBeInTheDocument();
    for (const name of ["重试", "解除"]) {
      expect(screen.queryByRole("button", { name: new RegExp(name) })).not.toBeInTheDocument();
    }
    for (const text of ["忽略", "隔离", "保留", "需要你处理"]) {
      expect(screen.queryByText(new RegExp(text))).not.toBeInTheDocument();
    }
    expect(screen.getByRole("button", { name: "立即探测" })).toBeInTheDocument();
  });

  it("shows container-first occupancy and the creation cooldown", async () => {
    renderPage();
    const strip = await screen.findByRole("region", { name: "资源占用" });
    expect(strip).toHaveTextContent("容器3/4");
    expect(strip).toHaveTextContent("运行 2 · 待起 1");
    expect(strip).toHaveTextContent("建题冷却");
    expect(screen.getByText(/暂不建新题：建题冷却中/)).toBeInTheDocument();
    // 容器优先：任务格只显示数量，不显示上限。
    expect(strip).toHaveTextContent("容器优先，不限任务数");
    expect(strip).not.toHaveTextContent("任务3/5");
  });

  it("shows the next 20 queue with a rotation preview", async () => {
    (api.dispatchUpcoming as ReturnType<typeof vi.fn>).mockResolvedValue({
      items: [
        {
          task_id: "preview:eng-2:0",
          task_name: "下一道候补工程",
          engineering_id: "eng-2",
          project_id: "project-1",
          preview: true,
          cycle: 0,
          used: 0,
          last_used_at: "",
        },
      ],
    });
    renderPage();
    expect(await screen.findByText("下一道候补工程")).toBeInTheDocument();
    expect(screen.getByText("QUEUE / NEXT 20")).toBeInTheDocument();
    expect(screen.getByText(/工程不足时循环补位/)).toBeInTheDocument();
  });

  it("switches between container-first and task-first modes", async () => {
    renderPage();
    const group = await screen.findByRole("radiogroup", { name: "调度模式" });
    const containerFirst = within(group).getByRole("radio", { name: "容器优先" });
    const taskFirst = within(group).getByRole("radio", { name: "任务优先" });
    expect(containerFirst).toHaveAttribute("aria-checked", "true");
    expect(screen.getByText("未启用")).toBeInTheDocument();
    fireEvent.click(taskFirst);
    expect(taskFirst).toHaveAttribute("aria-checked", "true");
    expect(screen.queryByText("未启用")).not.toBeInTheDocument();
    expect(screen.getByText(/任务优先：在手任务数小于最大任务数/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "应用" }));
    await waitFor(() => expect(api.dispatchLimits).toHaveBeenCalledTimes(1));
    expect((api.dispatchLimits as ReturnType<typeof vi.fn>).mock.calls[0][0]).toMatchObject({ mode: "tasks", max_tasks: 5 });
  });

  it("applies in-page limits and queue actions", async () => {
    renderPage();
    const containers = await screen.findByLabelText("最大容器数");
    fireEvent.change(containers, { target: { value: "5" } });
    fireEvent.change(screen.getByLabelText("最大任务数"), { target: { value: "7" } });
    fireEvent.change(screen.getByLabelText("建题冷却"), { target: { value: "240" } });
    expect(screen.queryByText("会话上限")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "应用" }));
    await waitFor(() => expect(api.dispatchLimits).toHaveBeenCalledTimes(1));
    expect((api.dispatchLimits as ReturnType<typeof vi.fn>).mock.calls[0][0]).toEqual({
      mode: "containers",
      containers: 5,
      max_tasks: 7,
      create_cooldown_seconds: 240,
    });
    fireEvent.click(screen.getByTitle("置顶"));
    await waitFor(() => expect(api.dispatchQueueAction).toHaveBeenCalledWith("task-1", "pin"));
  });
});

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <ToastProvider>
        <MemoryRouter>
          <DispatchPage />
        </MemoryRouter>
      </ToastProvider>
    </QueryClientProvider>,
  );
}
