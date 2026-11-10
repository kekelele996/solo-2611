import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowRight, CalendarDays, Play, Upload } from "lucide-react";
import { useMemo } from "react";
import { Link, useNavigate } from "react-router-dom";

import { api } from "../api/client";
import type { TaskRow } from "../api/types";
import { useToast } from "../components/toast-context";
import { Button, EmptyState, ErrorPanel, LoadingLines, Panel, StatusDot } from "../components/ui";
import { formatDateTime, todayShanghai } from "../utils/time";

const statusLabels: Record<string, string> = {
  blocked: "卡住",
  running: "运行中",
  queued: "排队中",
  ready: "可运行",
  prompt_ok: "待推快照",
  authored: "待出题",
  ran: "待录屏",
  recorded: "待写 Feedback",
  reviewed: "待导出",
  exported: "已导出",
  archived: "已归档",
  aborted: "已放弃",
  orphan: "状态丢失",
};

export function DashboardPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const today = todayShanghai();
  const tasks = useQuery({
    queryKey: ["tasks", "dashboard"],
    queryFn: () => api.tasks({ page_size: 200 }),
    refetchInterval: 3000,
  });
  const status = useQuery({
    queryKey: ["system-status"],
    queryFn: api.systemStatus,
    refetchInterval: 3000,
  });
  const queue = useQuery({ queryKey: ["queue"], queryFn: api.queue, refetchInterval: 3000 });

  const grouped = useMemo(() => {
    const rows = tasks.data?.items ?? [];
    return {
      blocked: rows.filter((task) => task.job_status === "blocked" || task.job_status === "orphan"),
      active: rows.filter((task) => task.job_status === "running" || task.job_status === "queued"),
      pending: rows.filter((task) =>
        ["authored", "prompt_ok", "ready", "ran", "recorded", "reviewed"].includes(task.job_status),
      ),
      completed: rows.filter((task) => task.job_status === "exported" || task.job_status === "archived"),
    };
  }, [tasks.data?.items]);

  const metrics = useMemo(() => {
    const rows = tasks.data?.items ?? [];
    const exportedToday = rows.filter(
      (task) => task.exported_at && formatDateTime(task.exported_at).slice(0, 10) === today,
    ).length;
    const waiting = rows.filter((task) =>
      ["authored", "prompt_ok", "ready", "ran", "recorded", "reviewed"].includes(task.job_status),
    ).length;
    const durations = rows
      .filter((task) => task.exported_at && task.created_at)
      .map((task) => (new Date(task.exported_at ?? "").getTime() - new Date(task.created_at).getTime()) / 60_000)
      .filter((minutes) => Number.isFinite(minutes) && minutes >= 0);
    const average = durations.length
      ? `${Math.round(durations.reduce((sum, minutes) => sum + minutes, 0) / durations.length)}m`
      : "—";
    return {
      exported: exportedToday,
      running: status.data?.running ?? 0,
      waiting,
      blocked: status.data?.blocked ?? 0,
      average,
    };
  }, [status.data, tasks.data?.items, today]);

  const batchReady = useMutation({
    mutationFn: async () => {
      const ready = (tasks.data?.items ?? []).filter((task) => task.job_status === "ready");
      const results = await Promise.allSettled(ready.map((task) => api.enqueue(task.id)));
      return {
        queued: results.filter((result) => result.status === "fulfilled").length,
        failed: results.filter((result) => result.status === "rejected").length,
      };
    },
    onSuccess: (result) => {
      toast(`已排队 ${result.queued} 题，跳过 ${result.failed} 题`, result.failed ? "warn" : "ok");
      void queryClient.invalidateQueries();
    },
    onError: (error) => toast(error instanceof Error ? error.message : String(error), "error"),
  });

  const exportToday = useMutation({
    mutationFn: () => api.export({ date: today }),
    onSuccess: (result) => {
      toast(`导出了 ${result.rows} 行，跳过 ${result.skipped.length} 题`, result.skipped.length ? "warn" : "ok");
      void queryClient.invalidateQueries();
      void navigate("/export");
    },
    onError: (error) => toast(error instanceof Error ? error.message : String(error), "error"),
  });

  return (
    <div className="page dashboard-page">
      <header className="page-head">
        <div>
          <span className="eyebrow">TODAY / {today}</span>
          <h1>2611 作业台</h1>
        </div>
        <div className="page-actions">
          <Button onClick={() => batchReady.mutate()} loading={batchReady.isPending}>
            <Play size={14} /> 批量排队
          </Button>
          <Button variant="primary" onClick={() => exportToday.mutate()} loading={exportToday.isPending}>
            <Upload size={14} /> 导出今日
          </Button>
        </div>
      </header>

      <div className="metric-strip">
        <Metric label="今日导出行" value={metrics.exported} />
        <Metric label="运行中" value={metrics.running} />
        <Metric label="待处理" value={metrics.waiting} />
        <Metric label="卡住" value={metrics.blocked} tone={metrics.blocked ? "danger" : undefined} />
        <Metric label="平均耗时" value={metrics.average} />
      </div>

      {tasks.isError ? <ErrorPanel error={tasks.error} onRetry={() => { void tasks.refetch(); }} /> : null}
      {tasks.isPending ? <LoadingLines rows={6} /> : null}
      {tasks.data && tasks.data.total === 0 ? (
        <EmptyState
          icon={<CalendarDays size={32} />}
          title="今天还没有任务。先选择一个项目领取工程。"
          action={<Button onClick={() => { void navigate("/source"); }}>去题源领取</Button>}
        />
      ) : null}

      {grouped.blocked.length ? (
        <TaskGroup title="卡住" tasks={grouped.blocked} onOpen={(id) => { void navigate(`/tasks/${id}`); }} />
      ) : null}
      {grouped.active.length ? (
        <TaskGroup title="进行中" tasks={grouped.active} onOpen={(id) => { void navigate(`/tasks/${id}`); }} queue={queue.data} />
      ) : null}
      {grouped.pending.length ? (
        <TaskGroup title="待处理" tasks={grouped.pending} onOpen={(id) => { void navigate(`/tasks/${id}`); }} />
      ) : null}
      {grouped.completed.length ? (
        <details className="completed-group">
          <summary>已完成与归档 · {grouped.completed.length}</summary>
          <TaskGroup title="" tasks={grouped.completed} onOpen={(id) => { void navigate(`/tasks/${id}`); }} />
        </details>
      ) : null}
    </div>
  );
}

function Metric({ label, value, tone }: { label: string; value: string | number; tone?: "danger" }) {
  return (
    <div className={`metric ${tone ? `metric-${tone}` : ""}`}>
      <strong>{value}</strong>
      <span>{label}</span>
    </div>
  );
}

function TaskGroup({
  title,
  tasks,
  onOpen,
  queue,
}: {
  title: string;
  tasks: TaskRow[];
  onOpen: (id: string) => void;
  queue?: { slots: Array<{ task_id: string; phase: string; attempt: number }> };
}) {
  return (
    <Panel className="task-group" title={title}>
      <div className="task-rows">
        {tasks.map((task) => {
          const slot = queue?.slots.find((item) => item.task_id === task.id);
          const next = nextAction(task);
          return (
            <div
              key={task.id}
              className={`task-row task-row-${task.job_status}`}
              tabIndex={0}
              role="link"
              onClick={() => onOpen(task.id)}
              onKeyDown={(event) => {
                if (event.key === "Enter") onOpen(task.id);
              }}
            >
              <div className="task-row-main">
                <StatusDot state={task.job_status} label={statusLabels[task.job_status] ?? task.job_status} />
                <Link to={`/tasks/${task.id}`} onClick={(event) => { event.stopPropagation(); }}>
                  {task.task_name}
                </Link>
                <small>{task.difficulty ?? "—"} · {task.task_type ?? "待定题"}</small>
              </div>
              <div className="task-row-progress">
                <div className="mini-progress">
                  <span style={{ width: `${progress(task)}%` }} />
                </div>
                <span>
                  {slot ? `attempt ${slot.attempt || 1}/3 · ${slot.phase}` : task.run?.phase ?? `${progress(task)}%`}
                </span>
              </div>
              <div className="task-row-action">
                <span>{next}</span>
                <ArrowRight size={15} />
              </div>
            </div>
          );
        })}
      </div>
    </Panel>
  );
}

function progress(task: TaskRow): number {
  const steps = task.steps ?? [];
  if (!steps.length) return 0;
  return Math.round((steps.filter((step) => step.state === "done").length / 11) * 100);
}

function nextAction(task: TaskRow): string {
  const mapping: Record<string, string> = {
    blocked: "查看阻塞原因",
    running: "查看运行",
    queued: "等待容器名额",
    authored: "继续写题面",
    prompt_ok: "推送初始快照",
    ready: "开始运行",
    ran: "录制证据",
    recorded: "完成 Feedback",
    reviewed: "导出 Excel",
    exported: "打开交付",
  };
  return mapping[task.job_status] ?? "继续处理";
}
