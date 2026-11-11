import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowRight, CalendarDays, Play, Upload } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { api } from "../api/client";
import { AutopilotPanel } from "../components/AutopilotPanel";
import { InboxPanel } from "../components/InboxPanel";
import type { TaskRow } from "../api/types";
import { useToast } from "../components/toast-context";
import { Button, EmptyState, ErrorPanel, LoadingLines, Panel, StatusDot } from "../components/ui";
import { errorText, formatDuration, statusLabel } from "../utils/display";
import { formatDateTime, todayShanghai } from "../utils/time";

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
  const batches = useQuery({ queryKey: ["batches"], queryFn: api.batches, refetchInterval: 5000 });

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
    const exportedToday = (batches.data?.items ?? [])
      .filter((batch) => formatDateTime(batch.created_at).slice(0, 10) === today)
      .reduce((sum, batch) => sum + (batch.rows || batch.task_ids.length), 0);
    const waiting = rows.filter((task) =>
      ["authored", "prompt_ok", "ready", "ran", "recorded", "reviewed"].includes(task.job_status),
    ).length;
    const durations = rows
      .filter((task) => task.exported_at && task.created_at)
      .map((task) => (new Date(task.exported_at ?? "").getTime() - new Date(task.created_at).getTime()) / 60_000)
      .filter((minutes) => Number.isFinite(minutes) && minutes >= 0);
    const average = durations.length
      ? formatDuration((durations.reduce((sum, minutes) => sum + minutes, 0) / durations.length) * 60)
      : "—";
    return {
      exported: exportedToday,
      running: status.data?.running ?? 0,
      waiting,
      blocked: status.data?.blocked ?? 0,
      average,
    };
  }, [batches.data?.items, status.data, tasks.data?.items, today]);

  const batchReady = useMutation({
    mutationFn: async () => {
      const ready = (tasks.data?.items ?? []).filter((task) => task.job_status === "ready");
      const results = await Promise.allSettled(ready.map((task) => api.enqueue(task.id)));
      const failed = results.filter((result) => result.status === "rejected");
      return { queued: results.length - failed.length, failed: failed.length, firstError: failed[0]?.reason as unknown };
    },
    onSuccess: (result) => {
      if (result.failed) toast(`已排队 ${result.queued} 题，${result.failed} 题未能排队：${errorText(result.firstError)}`, "warn");
      else toast(`已排队 ${result.queued} 题`, "ok");
      void queryClient.invalidateQueries();
    },
    onError: (error) => toast(errorText(error), "error"),
  });

  const exportToday = useMutation({
    mutationFn: () => api.export({ date: today }),
    onSuccess: (result) => {
      if (result.rows) toast(`导出了 ${result.rows} 行，跳过 ${result.skipped.length} 题`, result.skipped.length ? "warn" : "ok");
      else toast(`今天没有可导出的任务，跳过 ${result.skipped.length} 题`, "warn");
      void queryClient.invalidateQueries();
      void navigate("/export");
    },
    onError: (error) => toast(errorText(error), "error"),
  });

  const readyCount = (tasks.data?.items ?? []).filter((task) => task.job_status === "ready").length;

  return (
    <div className="page dashboard-page">
      <header className="page-head">
        <div>
          <span className="eyebrow">TODAY / {today}</span>
          <h1>2611 作业台</h1>
        </div>
        <div className="page-actions">
          <Button
            onClick={() => batchReady.mutate()}
            loading={batchReady.isPending}
            disabled={!readyCount}
            title={readyCount ? `把 ${readyCount} 个可运行任务送入队列` : "当前没有可运行的任务"}
          >
            <Play size={14} /> 批量排队{readyCount ? ` · ${readyCount}` : ""}
          </Button>
          <Button variant="primary" onClick={() => exportToday.mutate()} loading={exportToday.isPending}>
            <Upload size={14} /> 导出今日
          </Button>
        </div>
      </header>

      <InboxPanel />

      <div className="metric-strip">
        <Metric label="今日批次题数" value={metrics.exported} />
        <Metric label="运行中" value={metrics.running} />
        <Metric label="待处理" value={metrics.waiting} />
        <Metric label="卡住" value={metrics.blocked} tone={metrics.blocked ? "danger" : undefined} />
        <Metric label="平均耗时" value={metrics.average} />
      </div>

      <AutopilotPanel />

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
  // 数字不滚动；变大时只挂一个静态 ▲，2 秒后撤掉
  const previous = useRef(value);
  const [rose, setRose] = useState(false);
  useEffect(() => {
    const before = previous.current;
    previous.current = value;
    if (typeof value !== "number" || typeof before !== "number" || value <= before) return;
    setRose(true);
    const timer = window.setTimeout(() => setRose(false), 2000);
    return () => window.clearTimeout(timer);
  }, [value]);
  return (
    <div className={`metric ${tone ? `metric-${tone}` : ""}`}>
      <strong>
        {value}
        {rose ? <sup aria-label="刚刚增加">▲</sup> : null}
      </strong>
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
          const done = (task.steps ?? []).filter((step) => step.state === "done").length;
          const tone = done === 11 ? "ok" : task.job_status === "blocked" || task.job_status === "orphan" ? "danger" : undefined;
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
                <StatusDot state={task.job_status} label={statusLabel(task.job_status)} />
                <Link to={`/tasks/${task.id}`} onClick={(event) => { event.stopPropagation(); }}>
                  {task.task_name}
                </Link>
                <small>{task.difficulty ?? "—"} · {task.task_type ?? "待定题"}</small>
              </div>
              <div className="task-row-progress">
                <div className="mini-progress" data-tone={tone}>
                  <span style={{ width: `${progress(task)}%` }} />
                </div>
                <span className="mono">
                  {done}/11{slot ? ` · 第 ${slot.attempt || 1} 次 · ${slot.phase}` : ""}
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
    archived: "查看存档",
    orphan: "查看状态丢失原因",
  };
  return mapping[task.job_status] ?? "继续处理";
}
