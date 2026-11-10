import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowLeft,
  ChevronLeft,
  ChevronRight,
  Clipboard,
  FolderOpen,
  Info,
  ShieldAlert,
  X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";

import { api } from "../api/client";
import type { Step, TaskDetail } from "../api/types";
import { useHotkeys } from "../hooks/useHotkeys";
import {
  ArtifactsTab,
  ExportTab,
  FeedbackTab,
  OverviewTab,
  PromptTab,
  RecordingTab,
  TimelineTab,
} from "../components/task/TaskTabs";
import {
  Badge,
  Button,
  ErrorPanel,
  KeyValue,
  LoadingLines,
  Modal,
  SplitPane,
  StatusDot,
  StepRail,
} from "../components/ui";
import { useToast } from "../components/toast-context";

const tabs = [
  ["overview", "概览"],
  ["prompt", "题面"],
  ["timeline", "轨迹"],
  ["artifact", "产物"],
  ["recording", "录屏"],
  ["feedback", "Feedback"],
  ["export", "导出"],
] as const;

type Tab = (typeof tabs)[number][0];

export function TaskDetailPage() {
  const { id = "" } = useParams();
  const [params, setParams] = useSearchParams();
  const tab = normalizeTab(params.get("tab"));
  const setTab = useCallback((value: Tab) => {
    const nextParams = new URLSearchParams(params);
    nextParams.set("tab", value);
    setParams(nextParams, { replace: true });
  }, [params, setParams]);
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const [abandonOpen, setAbandonOpen] = useState(false);
  const [inspectorOpen, setInspectorOpen] = useState(false);
  const [abandonReason, setAbandonReason] = useState("");
  const task = useQuery({
    queryKey: ["task", id],
    queryFn: () => api.task(id),
    enabled: Boolean(id),
    refetchInterval: 3000,
  });
  const taskList = useQuery({
    queryKey: ["tasks", "detail-navigation"],
    queryFn: () => api.tasks({ page_size: 200 }),
  });
  const run = useMutation({
    mutationFn: () => api.enqueue(id),
    onSuccess: () => {
      toast("任务已进入运行队列", "ok");
      void queryClient.invalidateQueries();
    },
    onError: (error) => toast(error instanceof Error ? error.message : String(error), "error"),
  });
  const abandon = useMutation({
    mutationFn: async () => {
      if (!task.data) throw new Error("任务尚未加载");
      await api.abortTask(task.data.id, abandonReason.trim());
      if (task.data.claim_id) await api.releaseClaim(task.data.claim_id);
    },
    onSuccess: () => {
      toast("任务已放弃，容器已清理并归还工程", "warn");
      setAbandonOpen(false);
      setAbandonReason("");
      void queryClient.invalidateQueries();
    },
    onError: (error) => toast(error instanceof Error ? error.message : String(error), "error"),
  });

  const ordered = useMemo(() => taskList.data?.items ?? [], [taskList.data?.items]);
  const index = ordered.findIndex((item) => item.id === id);
  const previous = index > 0 ? ordered[index - 1] : null;
  const next = index >= 0 && index < ordered.length - 1 ? ordered[index + 1] : null;

  useHotkeys({
    onPrimary: () => clickPrimary(),
    onSave: () => clickSave(),
    onNext: () => {
      if (next) void navigate(`/tasks/${next.id}?tab=${tab}`);
    },
    onPrevious: () => {
      if (previous) void navigate(`/tasks/${previous.id}?tab=${tab}`);
    },
    onCopyEvidence: () => task.data && copyEvidence(task.data, tab, toast),
    onTaskRoot: () => {
      void api.reveal(id, "task_root").catch((error: unknown) =>
        toast(error instanceof Error ? error.message : String(error), "error"),
      );
    },
  });

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      if (target?.tagName === "INPUT" || target?.tagName === "TEXTAREA" || target?.closest(".cm-editor")) return;
      const number = Number(event.key);
      if (number >= 1 && number <= 7) {
        event.preventDefault();
        setTab(tabs[number - 1][0]);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [setTab]);

  useEffect(() => {
    if (!inspectorOpen) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setInspectorOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [inspectorOpen]);

  if (task.isPending) return <div className="page"><LoadingLines rows={10} /></div>;
  if (task.isError) return <div className="page"><ErrorPanel error={task.error} onRetry={() => { void task.refetch(); }} /></div>;
  const detail = task.data;
  const done = detail.steps.filter((step) => step.ok).length;
  const current = detail.steps.find((step) => !step.ok);

  const center = (
    <div className="task-workbench">
      <div className="task-titlebar">
        <div>
          <Link to="/tasks" className="back-link"><ArrowLeft size={13} /> 任务列表</Link>
          <h1>{detail.task_name}</h1>
          <p>{detail.domain} · {detail.sub_direction} · {detail.task_type ?? "待定题"} · {detail.difficulty ?? "难度待定"}</p>
        </div>
        <div className="task-pager">
          <Button className="evidence-toggle" size="small" onClick={() => setInspectorOpen(true)}>
            证据
          </Button>
          <Button size="small" variant="ghost" disabled={!previous} onClick={() => { if (previous) void navigate(`/tasks/${previous.id}?tab=${tab}`); }}>
            <ChevronLeft size={14} /> 上一题
          </Button>
          <Button size="small" variant="ghost" disabled={!next} onClick={() => { if (next) void navigate(`/tasks/${next.id}?tab=${tab}`); }}>
            下一题 <ChevronRight size={14} />
          </Button>
        </div>
      </div>
      {!detail.compatible ? (
        <div className="compatibility-banner">
          <ShieldAlert size={16} />
          <div>
            <strong>兼容模式：任务状态未解析</strong>
            <pre>{detail.compatibility_error}</pre>
          </div>
        </div>
      ) : null}
      <nav className="task-tabs" aria-label="任务详情标签页">
        {tabs.map(([value, label], tabIndex) => (
          <button
            key={value}
            aria-selected={tab === value}
            className={tab === value ? "is-active" : ""}
            onClick={() => setTab(value)}
          >
            <span>{tabIndex + 1}</span>
            {label}
          </button>
        ))}
      </nav>
      <div className="task-tab-content">
        {tab === "overview" ? <OverviewTab task={detail} onRun={() => run.mutate()} /> : null}
        {tab === "prompt" ? <PromptTab task={detail} /> : null}
        {tab === "timeline" ? <TimelineTab task={detail} /> : null}
        {tab === "artifact" ? <ArtifactsTab task={detail} /> : null}
        {tab === "recording" ? <RecordingTab task={detail} /> : null}
        {tab === "feedback" ? <FeedbackTab task={detail} /> : null}
        {tab === "export" ? <ExportTab task={detail} /> : null}
      </div>
    </div>
  );

  const left = (
    <div className="rail-panel">
      <div className="rail-head">
        <span>交付完整性</span>
        <strong>{done}/11</strong>
      </div>
      <StepRail steps={detail.steps} />
      <div className="next-block">
        <span>下一步</span>
        <strong>{current ? current.name : "归档并归还"}</strong>
        <p>{current?.detail || "所有检查项已通过。"}</p>
        <Button variant="primary" onClick={() => setTab(tabForStep(current))}>
          {nextLabel(current)}
        </Button>
      </div>
    </div>
  );

  const right = <Inspector task={detail} onAbandon={() => setAbandonOpen(true)} onReveal={() => {
    void api.reveal(detail.id, "task_root").catch((error: unknown) =>
      toast(error instanceof Error ? error.message : String(error), "error"),
    );
  }} />;

  return (
    <div className="task-detail-page">
      <SplitPane left={left} center={center} right={right} />
      <div className="mobile-task-toolbar">
        <Button onClick={() => setTab("overview")}><Info size={14} /> 概览</Button>
        <Button onClick={() => void api.reveal(id, "task_root")}><FolderOpen size={14} /> 目录</Button>
      </div>
      <Modal
        open={abandonOpen}
        title="放弃本题"
        onClose={() => setAbandonOpen(false)}
        footer={
          <>
            <Button onClick={() => setAbandonOpen(false)}>取消</Button>
            <Button
              variant="danger"
              disabled={!abandonReason.trim()}
              loading={abandon.isPending}
              onClick={() => abandon.mutate()}
            >
              放弃并归还
            </Button>
          </>
        }
      >
        <label className="field">
          <span className="field-label">原因</span>
          <textarea
            value={abandonReason}
            onChange={(event) => setAbandonReason(event.target.value)}
            placeholder="说明为什么放弃本题"
          />
        </label>
        <div className="gate-warning">
          操作会先清理本题容器，再归还工程；本地任务目录不会自动删除。
        </div>
      </Modal>
      {inspectorOpen ? (
        <div className="drawer-layer" onMouseDown={() => setInspectorOpen(false)}>
          <aside className="drawer inspector-drawer" onMouseDown={(event) => event.stopPropagation()}>
            <button className="icon-button drawer-close" aria-label="关闭证据栏" onClick={() => setInspectorOpen(false)}>
              <X size={16} />
            </button>
            {right}
          </aside>
        </div>
      ) : null}
    </div>
  );
}

function Inspector({ task, onReveal, onAbandon }: { task: TaskDetail; onReveal: () => void; onAbandon: () => void }) {
  const { toast } = useToast();
  const state = task.state as {
    session?: { sessionId?: string; promptId?: string; toolUses?: number };
    recording?: { durationSeconds?: number; width?: number; height?: number };
    remote?: { web?: string };
  };
  const recording = state.recording;
  const remoteWeb = state.remote?.web?.replace(/\/$/, "") ?? "";
  return (
    <div className="inspector-panel">
      <div className="inspector-head">
        <span>证据</span>
        <Badge tone={task.complete ? "ok" : task.job_status === "blocked" ? "danger" : "warn"}>
          {task.job_status}
        </Badge>
      </div>
      <div className="key-value-list">
        <CopyValue label="SessionID" value={state.session?.sessionId ?? ""} />
        <CopyValue label="PromptID" value={state.session?.promptId ?? ""} />
        <KeyValue label="初始快照" value={shortSha(task.initial_sha ?? "")} mono />
        <KeyValue label="产物快照" value={shortSha(task.artifact_sha ?? "")} mono />
        {remoteWeb && task.initial_sha ? (
          <KeyValue
            label="初始链接"
            value={<a href={`${remoteWeb}/commit/${task.initial_sha}`} target="_blank" rel="noreferrer">GitHub</a>}
          />
        ) : null}
        {remoteWeb && task.artifact_sha ? (
          <KeyValue
            label="产物链接"
            value={<a href={`${remoteWeb}/commit/${task.artifact_sha}`} target="_blank" rel="noreferrer">GitHub</a>}
          />
        ) : null}
        <KeyValue label="工具调用" value={state.session?.toolUses ?? "—"} mono />
        <KeyValue
          label="录屏"
          value={recording?.durationSeconds ? `${recording.durationSeconds}s ${recording.width}×${recording.height}` : "未录制"}
        />
        <KeyValue label="反馈" value={task.feedback_score ? `${task.feedback_score} 分` : "—"} />
        <KeyValue label="复核" value={task.review_acked_at ? "已确认" : "未确认"} />
      </div>
      <div className="inspector-actions">
        <Button onClick={onReveal}><FolderOpen size={14} /> 打开目录</Button>
        <Button
          onClick={() => {
            void api.reveal(task.id, "vscode").catch((error: unknown) =>
              toast(error instanceof Error ? error.message : String(error), "error"),
            );
          }}
        >
          在 VS Code 打开
        </Button>
        <Button
          variant="ghost"
          onClick={() => {
            void navigator.clipboard.writeText(task.task_root).then(() => toast("绝对路径已复制", "info"));
          }}
        >
          <Clipboard size={14} /> 复制路径
        </Button>
        {task.job_status !== "exported" && task.job_status !== "archived" ? (
          <Button variant="danger" onClick={onAbandon}>放弃并归还</Button>
        ) : null}
      </div>
      <div className="inspector-note">
        <StatusDot state={task.compatible ? "done" : "blocked"} label={task.compatible ? "磁盘事实可读" : "兼容模式"} />
        <small>{task.skill_status ?? "—"} · skill {task.skill_version ?? "—"}</small>
      </div>
    </div>
  );
}

function CopyValue({ label, value }: { label: string; value: string }) {
  const { toast } = useToast();
  return (
    <div className="key-value">
      <span>{label}</span>
      <strong className="mono ellipsis" title={value}>{value ? `${value.slice(0, 12)}…` : "—"}</strong>
      {value ? (
        <button
          className="icon-button"
          aria-label={`复制 ${label}`}
          onClick={() => {
            void navigator.clipboard.writeText(value).then(() => toast(`${label} 已复制`, "info"));
          }}
        >
          <Clipboard size={12} />
        </button>
      ) : null}
    </div>
  );
}

function normalizeTab(value: string | null): Tab {
  return tabs.some(([candidate]) => candidate === value) ? (value as Tab) : "overview";
}

function tabForStep(step?: Step): Tab {
  if (!step) return "export";
  const mapping: Record<number, Tab> = {
    1: "prompt",
    2: "prompt",
    3: "prompt",
    4: "overview",
    5: "overview",
    6: "artifact",
    7: "prompt",
    8: "timeline",
    9: "recording",
    10: "feedback",
    11: "export",
  };
  return mapping[step.number] ?? "overview";
}

function nextLabel(step?: Step): string {
  if (!step) return "进入导出";
  const mapping: Record<number, string> = {
    1: "检查初始快照",
    2: "推送初始快照",
    3: "安装题面",
    4: "执行单轮会话",
    5: "核对 PromptID",
    6: "查看产物快照",
    7: "推送产物快照",
    8: "生成时间线",
    9: "选择窗口录制",
    10: "完成 Feedback",
    11: "生成 Excel",
  };
  return mapping[step.number] ?? "继续处理";
}

function clickPrimary(): void {
  const target = document.querySelector<HTMLElement>("[data-primary]");
  target?.click();
}

function clickSave(): void {
  const target = document.querySelector<HTMLElement>("[data-save]") ?? document.querySelector<HTMLElement>("[data-primary]");
  target?.click();
}

function copyEvidence(task: TaskDetail, tab: Tab, toast: (message: string, tone?: "info" | "warn" | "error" | "ok") => void): void {
  const state = task.state as {
    session?: { sessionId?: string };
    artifact?: { shortStat?: string };
  };
  const value =
    tab === "feedback"
      ? String(task.feedback_score ?? "")
      : tab === "artifact"
        ? state.artifact?.shortStat ?? task.artifact_sha ?? ""
        : state.session?.sessionId ?? task.task_name;
  void navigator.clipboard.writeText(value).then(() => toast("当前 Tab 的关键证据已复制", "info"));
}

function shortSha(value: string): string {
  return value ? `${value.slice(0, 12)}…` : "—";
}
