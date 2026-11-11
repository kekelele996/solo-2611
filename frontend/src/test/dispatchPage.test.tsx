import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
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
  preset: "balanced",
  settings: { sessions: 0, authoring: 2, delivering: 1, in_hand: 0, session_spacing_seconds: 20 },
  limits: {
    containers: 4,
    effective_containers: 4,
    sessions: 5,
    in_hand: 5,
    launch_spacing_seconds: 45,
    recording: 1,
    skill_limit: 4,
    synced: true,
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
  elastic: { enabled: false, effective: 4, floor: 4, ceiling: 6 },
  loops: [],
  keep_awake: true,
};

describe("dispatch page", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api, "dispatch").mockResolvedValue(SNAPSHOT);
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
    vi.spyOn(api, "dispatchReleaseHold").mockResolvedValue({
      ok: true,
      source: "llm_guard",
      snapshot: SNAPSHOT,
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
    expect(await screen.findByRole("heading", { name: "调度" })).toBeInTheDocument();
    expect(screen.getByText(SNAPSHOT.sentence)).toBeInTheDocument();
    expect(screen.getByText("网关连续 2 次探测失败")).toBeInTheDocument();
    expect(screen.getByText("门禁连续两次返回相同错误")).toBeInTheDocument();
    fireEvent.click(screen.getByText("门禁连续两次返回相同错误"));
    expect(screen.getByText("WHY NOT RUNNING")).toBeInTheDocument();
    expect(screen.getByText("G12")).toBeInTheDocument();
  });

  it("applies in-page limits and queue actions", async () => {
    renderPage();
    const containers = await screen.findByLabelText("容器数");
    fireEvent.change(containers, { target: { value: "5" } });
    fireEvent.click(screen.getByRole("button", { name: "应用" }));
    await waitFor(() => expect(api.dispatchLimits).toHaveBeenCalledTimes(1));
    expect((api.dispatchLimits as ReturnType<typeof vi.fn>).mock.calls[0][0]).toMatchObject({ containers: 5 });
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
