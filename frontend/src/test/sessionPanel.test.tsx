import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../api/client";
import type { SessionStatus, TaskDetail } from "../api/types";
import { SessionPanel } from "../components/task/SessionPanel";
import { ToastProvider } from "../components/toast";

const SESSION: SessionStatus = {
  status: "running",
  startedAt: "2026-11-13T09:00:00+08:00",
  sessionState: "running",
  round: 2,
  rounds: [
    {
      no: 1,
      startedAt: "2026-11-13T09:00:00+08:00",
      endedAt: "2026-11-13T09:01:00+08:00",
      kind: "rate_limited",
      verdict: "continue",
      continued: true,
      detail: "429 Tokens Per Minute",
    },
    {
      no: 2,
      startedAt: "2026-11-13T09:02:00+08:00",
      kind: "none",
      verdict: "complete",
      toolUses: 41,
    },
  ],
  continues: { used: 1, max: 2, history: [] },
  lastKind: "none",
  lastActivityAt: "2026-11-13T09:02:00+08:00",
  nextActionAt: null,
  remainingSeconds: 3600,
  supervisorAlive: true,
  container: "solo2611-test",
  containerRunning: true,
  tmuxSession: "solo2611-test",
  tmuxAlive: true,
  sessionId: "",
  promptId: "",
  promptDelivery: null,
  error: "",
  blocked: null,
  abort: null,
  outcome: null,
  logTail: [],
  attempt: 1,
  mode: "supervised",
};

const TASK = {
  id: "task-1",
  task_name: "会话测试",
  state: {
    sessionRun: {
      mode: "supervised",
      status: "running",
      attemptIndex: 1,
      continues: { used: 1, max: 2 },
    },
  },
} as unknown as TaskDetail;

describe("SessionPanel", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api, "session").mockResolvedValue(SESSION);
  });
  afterEach(cleanup);

  it("renders rounds and consumes a confirmed continue", async () => {
    const send = vi.spyOn(api, "continueSession").mockResolvedValue({
      ok: true,
      sent: true,
      status: {
        ...SESSION,
        continues: { used: 2, max: 2, history: [] },
      },
    });
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <ToastProvider>
          <SessionPanel task={TASK} onAbort={() => undefined} />
        </ToastProvider>
      </QueryClientProvider>,
    );

    expect(await screen.findByText("第 2 轮 · 已继续 1/2")).toBeTruthy();
    expect(screen.getByText("429 Tokens Per Minute")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "继续" }));
    await waitFor(() => expect(send).toHaveBeenCalledWith("task-1"));
  });

  it("shows automatic prompt delivery repair before the first trace event", async () => {
    (api.session as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...SESSION,
      status: "prompt_delivery",
      sessionState: "running",
      rounds: [],
      promptDelivery: {
        status: "waiting_trace",
        startedAt: "2026-11-13T09:00:00+08:00",
        confirmedAt: "",
        trace: "",
        promptId: "",
        literalAttempts: 1,
        submitAttempts: 1,
        repairCount: 1,
        maxRepairCount: 4,
        lastAction: "submit",
        lastActionAt: "2026-11-13T09:00:08+08:00",
        error: "",
      },
    });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <ToastProvider>
          <SessionPanel task={TASK} onAbort={() => undefined} />
        </ToastProvider>
      </QueryClientProvider>,
    );

    expect(await screen.findByText("题面发送确认中")).toBeInTheDocument();
    expect(screen.getByText("题面尚未确认送达，已自动补发 1/4")).toBeInTheDocument();
  });
});
