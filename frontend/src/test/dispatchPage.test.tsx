import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../api/client";
import type { DispatchPreviewItem, DispatchSnapshot, IntakeSnapshot } from "../api/types";
import { ToastProvider } from "../components/toast";
import { DispatchPage } from "../pages/DispatchPage";

const SNAPSHOT: DispatchSnapshot = {
  engine: "v3",
  active_profile_name: "数仓评审组",
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
    effective_containers: 4,
    elastic: {
      enabled: false,
      ceiling: 8,
      step_up_seconds: 300,
      cooldown_seconds: 600,
      stall_percent: 3,
      severe_attempt: 6,
      stable_seconds: 1800,
    },
    max_tasks: 5,
    create_cooldown_seconds: 180,
    recording: 1,
    skill_limit: 4,
    synced: true,
  },
  elastic: {
    enabled: false,
    ceiling: 8,
    step_up_seconds: 300,
    cooldown_seconds: 600,
    stall_percent: 3,
    severe_attempt: 6,
    stable_seconds: 1800,
    floor: 4,
    current: 4,
    recent_rate_limits: 0,
    stall_percent_now: 0,
    worst_attempt: 0,
    learned_for_hour: 0,
    last_429_at: "",
    last_pressure_at: "",
    last_change_at: "",
    last_reason: "",
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
      status: "queued",
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

const INTAKE: IntakeSnapshot = {
  plan: {
    id: "plan-1", project_id: "project-1", status: "running", target: 0, filters: {},
    created_at: "", updated_at: "", task_ids: [], failed_claims: [],
  },
  counts: { target: 0, completed: 0, active: 0, failed: 0, recoverable: 0, orphaned: 0, claimed: 0, staged: 0, in_hand: 0 },
  max_in_hand: 1, eta: "",
};

const CANDIDATE: DispatchPreviewItem = {
  task_id: "preview:eng-2:0", task_name: "下一道候补工程", engineering_id: "eng-2",
  project_id: "project-1", preview: true, cycle: 0, used: 0, last_used_at: "",
};

describe("dispatch page", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api, "viewCodeStatus").mockResolvedValue({ configured: false });
    // jsdom 尚未实现原生模态对话框，真实键盘行为另在浏览器验证。
    HTMLDialogElement.prototype.showModal = function () { this.open = true; };
    HTMLDialogElement.prototype.close = function () { this.open = false; };
    vi.spyOn(api, "dispatch").mockResolvedValue(SNAPSHOT);
    vi.spyOn(api, "intake").mockResolvedValue({ ...INTAKE, plan: null });
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
    vi.spyOn(api, "openAllWorkTerminals").mockResolvedValue({
      ok: true,
      count: 3,
      opened: 0,
      reused: 3,
      errors: [],
    });
    vi.spyOn(api, "dispatchRecover").mockResolvedValue({ requeued: 1, preserved: 0, task_ids: ["task-1"] });
  });

  afterEach(cleanup);

  it("renders the state sentence, scoped hold and queue reason", async () => {
    renderPage();
    const title = await screen.findByRole("heading", { name: "任务队列" });
    const header = within(title.closest("header")!);
    expect(header.getByText("当前配置")).toBeVisible();
    expect(header.getByText("数仓评审组")).toBeVisible();
    expect(await screen.findByRole("button", {name: "保存查看码"})).toBeVisible();
    expect(screen.getByText(SNAPSHOT.sentence)).toBeInTheDocument();
    expect(screen.getByText("网关连续 2 次探测失败")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /待处理/ })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /正在处理/ })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: /已处理/ })).not.toBeInTheDocument();
    expect(screen.getByText("门禁连续两次返回相同错误")).toBeInTheDocument();
    expect(screen.getByText("自动重试 1/2")).toBeInTheDocument();
    fireEvent.click(screen.getByText("门禁连续两次返回相同错误"));
    expect(screen.getByRole("dialog", { name: /等待原因/ })).toBeInTheDocument();
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

  it("opens every working terminal from the dispatch header", async () => {
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: /查看全部终端/ }));
    await waitFor(() => expect(api.openAllWorkTerminals).toHaveBeenCalledTimes(1));
  });

  it("shows container-first occupancy and the creation cooldown", async () => {
    renderPage();
    const strip = await screen.findByRole("region", { name: "资源占用" });
    expect(strip).toHaveTextContent("容器2/4");
    expect(strip).toHaveTextContent("运行 2 · 待起 1");
    expect(strip).toHaveTextContent("建题冷却");
    expect(screen.getByText(/暂不建新题：建题冷却中/)).toBeInTheDocument();
    // 容器优先：任务格只显示数量，不显示上限。
    expect(strip).toHaveTextContent("容器优先，不限任务数");
    expect(strip).not.toHaveTextContent("任务3/5");
  });

  it("explains an empty pending queue using the live creation gate", async () => {
    const text = "容器名额已满（运行 1/1），等容器释放再建题";
    vi.mocked(api.dispatch).mockResolvedValue({
      ...SNAPSHOT, effective_state: "running", holds: [], queue: [],
      creation: { ...SNAPSHOT.creation!, code: "containers.full", text },
    });
    renderPage();
    expect(await screen.findByText("暂无待处理工程")).toBeInTheDocument();
    expect(screen.getByText(text)).toBeInTheDocument();
    expect(api.dispatchUpcoming).not.toHaveBeenCalled();
  });

  it("shows unique candidates after queued tasks and skips already scheduled engineering", async () => {
    vi.mocked(api.intake).mockResolvedValue(INTAKE);
    vi.mocked(api.dispatchUpcoming).mockResolvedValue({
      items: [
        { ...CANDIDATE, engineering_id: "eng-1", task_name: "已入队工程的候补" },
        CANDIDATE, { ...CANDIDATE, task_id: "preview:eng-2:1", cycle: 1 },
      ],
    });
    renderPage();
    const list = await screen.findByRole("list", { name: "候补工程" });
    expect(within(list).getAllByRole("listitem")).toHaveLength(1);
    expect(list).toHaveTextContent("下一道候补工程");
    expect(screen.getByText("列级血缘增量分析").compareDocumentPosition(list) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.getByRole("heading", { name: /待处理/ })).toHaveTextContent("1 已排队1 候补");
  });

  it("loads candidates from a cold preview cache when there are no queued tasks", async () => {
    vi.mocked(api.intake).mockResolvedValue(INTAKE);
    vi.mocked(api.dispatch).mockResolvedValue({ ...SNAPSHOT, queue: [] });
    vi.mocked(api.dispatchUpcoming).mockResolvedValueOnce({ items: [] }).mockResolvedValue({ items: [CANDIDATE] });
    renderPage();
    expect(await screen.findByText("下一道候补工程", {}, { timeout: 4000 })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /待处理/ })).toHaveTextContent("0 已排队1 候补");
    expect(screen.queryByText("暂无待处理工程")).not.toBeInTheDocument();
    expect(screen.getByText(SNAPSHOT.creation!.text)).toBeInTheDocument();
  });

  it("switches between container-first and task-first modes", async () => {
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: /调度参数/ }));
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
    expect((api.dispatchLimits as ReturnType<typeof vi.fn>).mock.calls[0][0]).toEqual({ mode: "tasks" });
  });

  it("applies in-page limits and queue actions", async () => {
    const second = { ...SNAPSHOT.queue[0], task_id: "task-2", queue_position: 2 };
    vi.mocked(api.dispatch).mockResolvedValue({ ...SNAPSHOT, queue: [...SNAPSHOT.queue, second] });
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: /调度参数/ }));
    expect(screen.getByLabelText("最大任务数")).toBeDisabled();
    fireEvent.click(screen.getByRole("radio", { name: "任务优先" }));
    const containers = await screen.findByLabelText("最大容器数");
    fireEvent.change(containers, { target: { value: "5" } });
    fireEvent.change(screen.getByLabelText("最大任务数"), { target: { value: "7" } });
    fireEvent.change(screen.getByLabelText("建题冷却"), { target: { value: "240" } });
    expect(screen.queryByText("会话上限")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "应用" }));
    await waitFor(() => expect(api.dispatchLimits).toHaveBeenCalledTimes(1));
    expect((api.dispatchLimits as ReturnType<typeof vi.fn>).mock.calls[0][0]).toEqual({
      mode: "tasks",
      containers: 5,
      max_tasks: 7,
      create_cooldown_seconds: 240,
    });
    expect(screen.getAllByTitle("置顶")[0]).toBeDisabled();
    fireEvent.click(screen.getAllByLabelText("更多操作")[1]);
    fireEvent.click(screen.getAllByTitle("置顶")[1]);
    await waitFor(() => expect(api.dispatchQueueAction).toHaveBeenCalledWith("task-2", "pin"));
  });

  it("enables elastic containers and applies the ceiling with the limits", async () => {
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: /调度参数/ }));
    fireEvent.click(await screen.findByRole("checkbox", { name: "启用弹性扩容" }));
    fireEvent.change(screen.getByLabelText("弹性上限"), { target: { value: "6" } });
    fireEvent.click(screen.getByRole("button", { name: "应用" }));
    await waitFor(() => expect(api.dispatchLimits).toHaveBeenCalledTimes(1));
    expect((api.dispatchLimits as ReturnType<typeof vi.fn>).mock.calls[0][0]).toEqual({
      elastic: { enabled: true, ceiling: 6 },
    });
  });
  it("preserves local edits across refresh and submits only changed fields", async () => {
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: /调度参数/ }));
    const cooldown = screen.getByLabelText("建题冷却");
    fireEvent.change(cooldown, { target: { value: "5" } });
    expect(screen.getByRole("button", { name: "应用" })).toBeDisabled();
    fireEvent.change(cooldown, { target: { value: "250" } });
    vi.mocked(api.dispatch).mockResolvedValue({ ...SNAPSHOT, limits: { ...SNAPSHOT.limits, containers: 6 } });
    fireEvent.click(screen.getByRole("button", { name: "刷新队列" }));
    await waitFor(() => expect(screen.getByLabelText("最大容器数")).toHaveValue(6));
    expect(cooldown).toHaveValue(250);
    fireEvent.click(screen.getByRole("button", { name: "应用" }));
    await waitFor(() => expect(api.dispatchLimits).toHaveBeenCalledWith({ create_cooldown_seconds: 250 }));
  });

  it("loads metrics on demand and marks only the first pending task", async () => {
    vi.spyOn(api, "dispatchThroughput").mockResolvedValue(SNAPSHOT.throughput);
    vi.spyOn(api, "sessionStats").mockResolvedValue({ accepted: 2, acceptedWithContinue: 1, continueRate: 0.5, sessionsPerContainerHour: 1, abortReasons: [] });
    const first = SNAPSHOT.queue[0];
    vi.mocked(api.dispatch).mockResolvedValue({ ...SNAPSHOT, queue: [first, { ...first, task_id: "second", queue_position: 2 }] });
    renderPage();
    expect(await screen.findAllByText("队首")).toHaveLength(1);
    expect(api.dispatchThroughput).not.toHaveBeenCalled();
    expect(api.sessionStats).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: /运行监控/ }));
    expect(await screen.findByText("87%")).toBeVisible();
    expect(api.dispatchThroughput).toHaveBeenCalledWith(24);
    expect(api.sessionStats).toHaveBeenCalledTimes(1);
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
