import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowLeft,
  ChevronLeft,
  ChevronRight,
  FolderOpen,
  PanelRight,
  ShieldAlert,
  X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, Navigate, useNavigate, useParams, useSearchParams } from "react-router-dom";

import { api } from "../api/client";
import { PurgeModal } from "../components/PurgeModal";
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
  TabBar,
} from "../components/ui";
import { useToast } from "../components/toast-context";
import { errorText, statusLabel, statusTone } from "../utils/display";

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
  const [purgeOpen, setPurgeOpen] = useState(false);
  const [purged, setPurged] = useState(false);
  const [inspectorOpen, setInspectorOpen] = useState(false);
  const [inspectorCollapsed, setInspectorCollapsed] = useState(false);
  const [abandonReason, setAbandonReason] = useState("");
  const task = useQuery({
    queryKey: ["task", id],
    queryFn: () => api.task(id),
    enabled: Boolean(id) && !purged,
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
    onError: (error) => toast(errorText(error), "error"),
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
    onError: (error) => toast(errorText(error), "error"),
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
        toast(errorText(error), "error"),
      );
    },
  });

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.metaKey || event.ctrlKey || event.altKey || event.defaultPrevented) return;
      const target = event.target as HTMLElement | null;
      if (target?.closest("input, textarea, select, [contenteditable], .cm-editor, [role='dialog'], [role='alertdialog']")) return;
      if (document.querySelector("[role='dialog'], [role='alertdialog']")) return;
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

  // 任务被清理后立刻离开，别让这一页继续轮询一个已经不存在的任务
  if (purged) return <Navigate to="/tasks" replace />;
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
          <Button
            className="evidence-toggle"
            size="small"
            variant="ghost"
            aria-pressed={!inspectorCollapsed}
            title="显示/隐藏证据栏"
            onClick={() => {
              // 窄屏下右栏不在布局里，改用抽屉
              if (window.matchMedia("(max-width: 1279px)").matches) setInspectorOpen(true);
              else setInspectorCollapsed((value) => !value);
            }}
          >
            <PanelRight size={14} /> 证据
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
      <TabBar tabs={tabs} value={tab} onChange={setTab} label="任务详情标签页" />
      <div className="task-tab-content">
        {tab === "overview" ? <OverviewTab task={detail} onRun={() => run.mutate()} running={run.isPending} /> : null}
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
      <StepRail steps={detail.steps} onSelect={(step) => setTab(tabForStep(step))} />
      <div className="next-block">
        <span>下一步</span>
        <strong>{current ? `Step ${current.number} · ${current.name}` : "全部通过"}</strong>
        <p>{current ? detail.missing[0] ?? current.detail : "11 项检查已通过，可以导出并归档归还。"}</p>
        {/* 只是跳转到对应 Tab；每屏唯一的主按钮留给 Tab 里真正执行动作的那个 */}
        <Button onClick={() => setTab(tabForStep(current))}>
          {nextLabel(current)} <ChevronRight size={14} />
        </Button>
      </div>
    </div>
  );

  const right = <Inspector task={detail} onAbandon={() => setAbandonOpen(true)} onPurge={() => setPurgeOpen(true)} onReveal={() => {
    void api.reveal(detail.id, "task_root").catch((error: unknown) =>
      toast(errorText(error), "error"),
    );
  }} />;

  return (
    <div className="task-detail-page">
      <SplitPane left={left} center={center} right={right} rightCollapsed={inspectorCollapsed} />
      <PurgeModal
        taskIds={[detail.id]}
        open={purgeOpen}
        onClose={() => setPurgeOpen(false)}
        onDone={(removed) => {
          setPurgeOpen(false);
          if (removed.length) setPurged(true);
        }}
      />
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

function Inspector({
  task,
  onReveal,
  onAbandon,
  onPurge,
}: {
  task: TaskDetail;
  onReveal: () => void;
  onAbandon: () => void;
  onPurge: () => void;
}) {
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
        <Badge tone={task.complete ? "ok" : statusTone(task.job_status)}>{statusLabel(task.job_status)}</Badge>
      </div>
      <div className="key-value-list">
        <KeyValue label="SessionID" value={shortSha(state.session?.sessionId ?? "")} mono copy={state.session?.sessionId} />
        <KeyValue label="PromptID" value={shortSha(state.session?.promptId ?? "")} mono copy={state.session?.promptId} />
        <KeyValue label="初始快照" value={shortSha(task.initial_sha ?? "")} mono copy={task.initial_sha ?? undefined} />
        <KeyValue label="产物快照" value={shortSha(task.artifact_sha ?? "")} mono copy={task.artifact_sha ?? undefined} />
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
              toast(errorText(error), "error"),
            );
          }}
        >
          在 VS Code 打开
        </Button>
        {task.job_status !== "exported" && task.job_status !== "archived" ? (
          <Button variant="danger" onClick={onAbandon}>放弃并归还</Button>
        ) : null}
        <Button variant="danger" onClick={onPurge}>清理任务</Button>
      </div>
      <div className="inspector-note">
        <StatusDot state={task.compatible ? "done" : "blocked"} label={task.compatible ? "磁盘事实可读" : "兼容模式"} />
        <small>{task.skill_status ?? "—"} · skill {task.skill_version ?? "—"}</small>
      </div>
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
  if (!value) {
    toast("当前 Tab 还没有可复制的证据", "warn");
    return;
  }
  navigator.clipboard.writeText(value).then(
    () => toast("当前 Tab 的关键证据已复制", "info"),
    (error: unknown) => toast(`复制失败：${errorText(error)}`, "error"),
  );
}

function shortSha(value: string): string {
  return value ? `${value.slice(0, 12)}…` : "—";
}
