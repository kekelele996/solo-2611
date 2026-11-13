import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Activity,
  CirclePause,
  CirclePlay,
  Clock3,
  FileWarning,
  ListRestart,
  MessageCircle,
  Play,
  RefreshCw,
  Square,
  Terminal,
  WandSparkles,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";

import { api } from "../api/client";
import type { AutopilotJob, AutopilotSnapshot } from "../api/types";
import { IntakePanel, PlanModal } from "../components/IntakePanel";
import { useToast } from "../components/toast-context";
import { Badge, Button, EmptyState, ErrorPanel, LoadingLines, Modal, StatusDot } from "../components/ui";
import { errorText } from "../utils/display";
import { formatDateTime } from "../utils/time";

const COLUMNS = [
  { key: "authoring", label: "起草" },
  { key: "running", label: "运行" },
  { key: "delivering", label: "交付" },
  { key: "awaiting_audit", label: "待抽检" },
  { key: "done", label: "完成" },
  { key: "failed", label: "失败" },
] as const;

const FAILURE_LABELS: Record<string, string> = {
  agent_timeout: "Agent 超时",
  agent_reported: "Agent 自报失败",
  gate_rejected: "门禁未过",
  run_incomplete: "运行结构不完整",
  auth: "鉴权失败",
  recording: "录屏失败",
  export: "导出失败",
  environment: "环境问题",
};

export function ProductionPage() {
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const [preflightOpen, setPreflightOpen] = useState(false);
  const [planOpen, setPlanOpen] = useState(false);
  const [failureFilter, setFailureFilter] = useState("");
  const pilot = useQuery({ queryKey: ["autopilot"], queryFn: api.autopilot, refetchInterval: 3000 });
  const preflight = useMutation({
    mutationFn: api.autopilotPreflight,
    onSuccess: () => setPreflightOpen(true),
    onError: (error) => toast(errorText(error), "error"),
  });
  const pause = useMutation({
    mutationFn: api.autopilotPause,
    onSuccess: () => {
      toast("自动交付和生产计划已暂停；正在运行的阶段会正常结束", "info");
      void queryClient.invalidateQueries();
    },
    onError: (error) => toast(errorText(error), "error"),
  });
  const resume = useMutation({
    mutationFn: api.autopilotResume,
    onSuccess: (result) => {
      const recovered = result.recovered?.length ?? 0;
      toast(
        recovered
          ? `已开始，重新拉起 ${recovered} 个中断任务`
          : "已开始，当前没有需要恢复的中断任务",
        "ok",
      );
      void queryClient.invalidateQueries();
    },
    onError: (error) => toast(errorText(error), "error"),
  });
  const stop = useMutation({
    mutationFn: () => api.autopilotStop([]),
    onSuccess: (result) => {
      toast(`已停止 ${result.stopped.length} 题并冻结生产计划`, "info");
      void queryClient.invalidateQueries();
    },
    onError: (error) => toast(errorText(error), "error"),
  });
  const retry = useMutation({
    mutationFn: ({ id, stage }: { id: string; stage: string }) => api.autopilotRetry(id, stage),
    onSuccess: () => {
      toast("已按磁盘事实重新排队该阶段", "ok");
      void queryClient.invalidateQueries();
    },
    onError: (error) => toast(errorText(error), "error"),
  });
  const batchRetry = useMutation({
    mutationFn: async (targets: AutopilotJob[]) => {
      const results = await Promise.allSettled(
        targets.map((job) => {
          const stage = retryStage(job);
          if (!stage) throw new Error(`${job.task_name} 没有可续跑阶段`);
          return api.autopilotRetry(job.task_id, stage);
        }),
      );
      return {
        done: results.filter((result) => result.status === "fulfilled").length,
        failed: results.filter((result) => result.status === "rejected").length,
      };
    },
    onSuccess: (result) => {
      toast(`批量续跑：成功 ${result.done} 题${result.failed ? `，失败 ${result.failed} 题` : ""}`, result.failed ? "warn" : "ok");
      void queryClient.invalidateQueries();
    },
  });
  const openChatGPT = useMutation({
    mutationFn: api.openChatGPT,
    onError: (error) => toast(errorText(error), "error"),
  });
  const jobs = useMemo(() => pilot.data?.jobs ?? [], [pilot.data?.jobs]);
  const grouped = useMemo(() => {
    const result = new Map<string, AutopilotJob[]>(COLUMNS.map((column) => [column.key, []]));
    for (const job of jobs) {
      const key =
        job.stage === "pending" || job.stage === "chatgpt"
          ? "authoring"
          : job.stage === "stopped"
            ? "failed"
            : job.stage;
      result.get(key)?.push(job);
    }
    return result;
  }, [jobs]);
  const failureGroups = useMemo(() => {
    const counts = new Map<string, number>();
    for (const job of jobs.filter((item) => item.stage === "failed")) {
      counts.set(job.failure_kind || "unknown", (counts.get(job.failure_kind || "unknown") ?? 0) + 1);
    }
    return [...counts.entries()];
  }, [jobs]);
  const todayDone = jobs.filter(
    (job) =>
      ["done", "awaiting_audit"].includes(job.stage) &&
      formatDateTime(job.updated_at).slice(0, 10) === formatDateTime(new Date().toISOString()).slice(0, 10),
  ).length;

  if (pilot.isPending) return <div className="page"><LoadingLines rows={8} /></div>;
  if (pilot.isError) return <div className="page"><ErrorPanel error={pilot.error} onRetry={() => void pilot.refetch()} /></div>;
  const snapshot: AutopilotSnapshot = pilot.data;

  return (
    <div className="page production-page">
      <header className="page-head">
        <div>
          <span className="eyebrow">UNATTENDED / DISK IS THE TRUTH</span>
          <h1>生产线</h1>
        </div>
        <div className="page-actions">
          <span className="production-today">今天交付 <strong>{todayDone}</strong> 题</span>
          <Button size="small" onClick={() => preflight.mutate()} loading={preflight.isPending}>
            <Activity size={13} /> 预检
          </Button>
          {snapshot.paused ? (
            <Button size="small" variant="primary" onClick={() => resume.mutate()} loading={resume.isPending}>
              <CirclePlay size={13} /> 开始并恢复
            </Button>
          ) : (
            <Button size="small" onClick={() => pause.mutate()} loading={pause.isPending}>
              <CirclePause size={13} /> 暂停
            </Button>
          )}
          <Button size="small" variant="danger" onClick={() => stop.mutate()} loading={stop.isPending} disabled={!snapshot.active}>
            <Square size={12} /> 全部停止
          </Button>
          <Button size="small" variant="primary" onClick={() => setPlanOpen(true)}>
            <WandSparkles size={13} /> 下达生产计划
          </Button>
        </div>
      </header>

      {snapshot.paused ? (
        <div className="production-alert" role="alert">
          <FileWarning size={17} />
          <div>
            <strong>{snapshot.paused_by_breaker ? `自动交付已熔断：${FAILURE_LABELS[snapshot.breaker.kind] ?? snapshot.breaker.kind}` : "自动交付已暂停"}</strong>
            <span>{snapshot.breaker.since ? `自 ${formatDateTime(snapshot.breaker.since)} 起不再拉起新阶段` : "已在运行的阶段会正常结束"}</span>
          </div>
          <Button size="small" onClick={() => resume.mutate()}>开始并恢复中断任务</Button>
        </div>
      ) : null}

      <IntakePanel />

      <div className="production-toolbar">
        <span className="mono">
          {snapshot.agent_mode === "chatgpt"
            ? `ChatGPT 桌面 ${snapshot.chatgpt_available ? "可用" : "不可用"} · 并发 ${snapshot.parallel_chatgpt} · 总时限 ${Math.round(snapshot.task_timeout_seconds / 60)} 分钟 · ${Math.round(snapshot.chatgpt_timeout_seconds / 60)} 分钟有效进展超时`
            : `CLI Agent ${snapshot.agent_available ? "可用" : "缺失"} · 起草并发 ${snapshot.parallel_authoring} · 交付并发 ${snapshot.parallel_delivering}`}
        </span>
        <label>
          失败类别
          <select value={failureFilter} onChange={(event) => setFailureFilter(event.target.value)}>
            <option value="">全部</option>
            {failureGroups.map(([kind, count]) => (
              <option key={kind} value={kind}>{FAILURE_LABELS[kind] ?? kind} · {count}</option>
            ))}
          </select>
        </label>
        <Button size="small" variant="ghost" onClick={() => void pilot.refetch()}><RefreshCw size={13} /> 刷新</Button>
      </div>
      {failureGroups.length ? (
        <div className="failure-groups">
          {failureGroups.map(([kind, count]) => {
            const targets = jobs.filter((job) => job.stage === "failed" && job.failure_kind === kind);
            return (
              <div key={kind}>
                <FileWarning size={13} />
                <strong>{FAILURE_LABELS[kind] ?? kind}</strong>
                <span>{count} 题</span>
                <Button size="small" variant="ghost" loading={batchRetry.isPending} onClick={() => batchRetry.mutate(targets)}>
                  <ListRestart size={12} /> 本阶段批量续跑
                </Button>
              </div>
            );
          })}
        </div>
      ) : null}

      {!jobs.length ? (
        <EmptyState
          icon={<WandSparkles size={30} />}
          title="生产线空闲。可以从任务页选择任务加入自动交付，或直接下达生产计划。"
          action={<Button onClick={() => setPlanOpen(true)}>下达计划</Button>}
        />
      ) : (
        <div className="production-board">
          {COLUMNS.map((column) => {
            const items = (grouped.get(column.key) ?? []).filter(
              (job) => !failureFilter || (column.key === "failed" && job.failure_kind === failureFilter),
            );
            return (
              <section className="production-column" key={column.key} data-stage={column.key}>
                <header>
                  <strong>{column.label}</strong>
                  <span>{items.length}</span>
                </header>
                <div className="production-cards">
                  {items.slice(0, 8).map((job) => (
                    <JobCard
                      key={job.task_id}
                      job={job}
                      onRetry={(stage) => retry.mutate({ id: job.task_id, stage })}
                      retrying={retry.isPending && retry.variables?.id === job.task_id}
                      onOpenChatGPT={() => openChatGPT.mutate(job.task_id)}
                      openingChatGPT={openChatGPT.isPending && openChatGPT.variables === job.task_id}
                    />
                  ))}
                  {items.length > 8 ? <div className="production-more">另有 {items.length - 8} 题</div> : null}
                  {!items.length ? <div className="production-empty">暂无</div> : null}
                </div>
              </section>
            );
          })}
        </div>
      )}

      <PreflightModal open={preflightOpen} onClose={() => setPreflightOpen(false)} result={preflight.data} />
      <PlanModal
        open={planOpen}
        onClose={() => setPlanOpen(false)}
        onCreated={() => {
          setPlanOpen(false);
          void queryClient.invalidateQueries();
        }}
      />
    </div>
  );
}

function JobCard({
  job,
  onRetry,
  retrying,
  onOpenChatGPT,
  openingChatGPT,
}: {
  job: AutopilotJob;
  onRetry: (stage: string) => void;
  retrying: boolean;
  onOpenChatGPT: () => void;
  openingChatGPT: boolean;
}) {
  const [logOpen, setLogOpen] = useState(job.stage === "chatgpt" && job.desktop_running);
  useEffect(() => {
    if (job.stage === "chatgpt" && job.desktop_running) setLogOpen(true);
  }, [job.desktop_running, job.stage]);
  const failed = job.stage === "failed";
  const recoverable = failed || job.stage === "stopped";
  return (
    <article className={`production-card ${failed ? "is-failed" : ""}`}>
      <div className="production-card-head">
        <StatusDot
          state={failed ? "blocked" : job.stage === "done" ? "exported" : job.stage === "awaiting_audit" ? "warn" : "running"}
          label={failed ? (FAILURE_LABELS[job.failure_kind] ?? "失败") : job.stage_label}
          pulse={job.active && job.agent_running}
        />
        <span className="mono"><Clock3 size={11} /> {formatElapsed(job.elapsed_seconds)}</span>
      </div>
      <Link to={`/tasks/${job.task_id}`} className="production-name" title={job.task_name}>{job.task_name}</Link>
      <p title={job.detail}>{job.detail}</p>
      <div className="production-card-meta">
        <span className="mono">
          {Object.entries(job.attempts).map(([stage, count]) => `${stage}×${count}`).join(" ") || "首次"}
        </span>
        {job.stage === "awaiting_audit" ? <Link to={`/audit?task=${job.task_id}`}><Badge tone="warn">去抽检</Badge></Link> : null}
        {job.audit?.verdict === "pass" ? <Badge tone="ok">抽检通过</Badge> : null}
        {job.desktop_running ? (
          <span className="production-live" title={job.desktop_last_progress_at || "等待日志"}>
            日志 {job.desktop_last_progress_at ? formatDateTime(job.desktop_last_progress_at, true).slice(11) : "连接中"}
          </span>
        ) : null}
      </div>
      {job.desktop_running || job.desktop_rollout ? (
        <Button size="small" variant="ghost" loading={openingChatGPT} onClick={onOpenChatGPT}>
          <MessageCircle size={12} /> 打开 ChatGPT 会话
        </Button>
      ) : null}
      {job.log ? (
        <button type="button" className="production-log-toggle" onClick={() => setLogOpen((open) => !open)}>
          <Terminal size={12} /> {logOpen ? "收起实时日志" : "展开实时日志"}
        </button>
      ) : null}
      {logOpen ? <InlineLog job={job} /> : null}
      {recoverable ? (
        <details className="retry-menu">
          <summary><ListRestart size={12} /> 重试</summary>
          <div>
            {job.retry_options.map((option) => (
              <button
                key={option.stage}
                type="button"
                disabled={!option.available || retrying}
                title={option.reason}
                onClick={() => onRetry(option.stage)}
              >
                <span>{option.label}</span>
                {!option.available ? <small>{option.reason}</small> : <Play size={11} />}
              </button>
            ))}
          </div>
        </details>
      ) : null}
    </article>
  );
}

function InlineLog({ job }: { job: AutopilotJob }) {
  const [filters, setFilters] = useState<Set<string>>(new Set(["message", "command", "thinking", "error"]));
  const [follow, setFollow] = useState(true);
  const [live, setLive] = useState<Array<{ kind: string; text: string }>>([]);
  const body = useRef<HTMLDivElement>(null);
  const log = useQuery({
    queryKey: ["autopilot-log", job.task_id, job.stage],
    queryFn: () => api.autopilotLog(job.task_id, job.stage),
    refetchInterval: job.agent_running ? 3000 : false,
  });
  useEffect(() => {
    if (log.data?.lines) setLive(log.data.lines);
  }, [log.data]);
  useEffect(() => {
    const source = new EventSource("/api/events/stream");
    source.addEventListener("autopilot.log", (event) => {
      try {
        const payload = JSON.parse((event as MessageEvent<string>).data) as {
          task_id?: string;
          frames?: Array<{ kind: string; text: string }>;
        };
        if (payload.task_id === job.task_id && payload.frames?.length) {
          setLive((current) => [...current, ...payload.frames!].slice(-500));
        }
      } catch {
        return;
      }
    });
    return () => source.close();
  }, [job.task_id]);
  useEffect(() => {
    if (follow) body.current?.scrollTo?.({ top: body.current.scrollHeight });
  }, [live, follow]);
  const visible = live.filter((frame) => filters.has(frame.kind) || (frame.kind === "other" && filters.has("message")));
  return (
    <div className="inline-log">
      <div className="inline-log-toolbar">
        {[
          ["message", "消息"],
          ["command", "命令"],
          ["thinking", "推理"],
          ["error", "错误"],
        ].map(([kind, label]) => (
          <button
            type="button"
            key={kind}
            data-active={filters.has(kind) || undefined}
            onClick={() =>
              setFilters((current) => {
                const next = new Set(current);
                if (next.has(kind)) next.delete(kind);
                else next.add(kind);
                return next;
              })
            }
          >
            {label}
          </button>
        ))}
        <a href={`/api/autopilot/${encodeURIComponent(job.task_id)}/log?stage=${encodeURIComponent(job.stage)}&download=1`}>
          下载 JSONL
        </a>
      </div>
      <div
        className="inline-log-body"
        ref={body}
        onScroll={(event) => {
          const element = event.currentTarget;
          setFollow(element.scrollHeight - element.scrollTop - element.clientHeight < 24);
        }}
      >
        {visible.map((frame, index) => (
          <div key={`${index}-${frame.kind}`} data-kind={frame.kind}>
            <span>{frame.kind}</span>
            <pre>{frame.text}</pre>
          </div>
        ))}
        {!visible.length ? <p>该阶段还没有可显示的日志。</p> : null}
      </div>
      {!follow ? <button type="button" className="back-to-latest" onClick={() => setFollow(true)}>回到最新</button> : null}
    </div>
  );
}

function PreflightModal({
  open,
  onClose,
  result,
}: {
  open: boolean;
  onClose: () => void;
  result?: { ok: boolean; items: Array<{ key: string; level: string; ok: boolean; message: string; fix: string }> };
}) {
  return (
    <Modal open={open} title="自动交付预检" onClose={onClose} footer={<Button onClick={onClose}>关闭</Button>}>
      {!result ? <LoadingLines rows={7} /> : (
        <div className="preflight-list">
          {result.items.map((item) => (
            <div key={item.key} className={item.ok ? "is-ok" : "is-bad"}>
              <StatusDot state={item.ok ? "done" : "blocked"} label={item.ok ? "通过" : "阻断"} />
              <strong>{item.key}</strong>
              <span>{item.message}</span>
              {!item.ok ? <small>{item.fix}</small> : null}
            </div>
          ))}
        </div>
      )}
    </Modal>
  );
}

function formatElapsed(seconds: number): string {
  const value = Math.max(0, Math.round(seconds || 0));
  if (value < 60) return `${value}s`;
  if (value < 3600) return `${Math.floor(value / 60)}m ${value % 60}s`;
  return `${Math.floor(value / 3600)}h ${Math.floor((value % 3600) / 60)}m`;
}

function retryStage(job: AutopilotJob): string {
  const failed = job.stages.find((stage) => stage.outcome === "failed" || stage.failure_kind);
  if (failed && job.retry_stages.includes(failed.stage)) return failed.stage;
  return job.retry_stages.at(-1) ?? "";
}
