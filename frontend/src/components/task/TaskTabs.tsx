import { markdown } from "@codemirror/lang-markdown";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useVirtualizer } from "@tanstack/react-virtual";
import CodeMirror, { EditorView } from "@uiw/react-codemirror";
import {
  AlertTriangle,
  Check,
  ChevronRight,
  Copy,
  Download,
  FileCode2,
  MonitorPlay,
  Pause,
  Play,
  Save,
  Search,
  ShieldCheck,
  Square,
  StopCircle,
  Upload,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import { api } from "../../api/client";
import type { ExportResult, PromptPreview, Step, TaskDetail, TimelineStep, WindowRow } from "../../api/types";
import { useDraft } from "../../hooks/useDraft";
import { useUIStore } from "../../stores/ui";
import { errorText, formatDuration, shortDetail, splitPath, statusLabel } from "../../utils/display";
import { formatClock } from "../../utils/time";
import { useToast } from "../toast-context";
import {
  Badge,
  Button,
  CodeBlock,
  ConfirmModal,
  EmptyState,
  ErrorPanel,
  Field,
  KeyValue,
  LoadingLines,
  Meter,
  Modal,
  Panel,
  StatusDot,
} from "../ui";

const taskTypes = [
  "功能开发",
  "Bugfix",
  "代码重构",
  "功能优化",
  "依赖升级与迁移",
  "质量保障",
  "项目维护",
  "技术讨论",
  "问题排查与诊断",
  "部署运维",
  "泛 Agent 需求",
];

const editorExtensions = [markdown(), EditorView.lineWrapping];

/** 容器心跳行：只说明「还活着」，不携带排错信息，默认折叠。 */
const HEARTBEAT = /thinking_tokens/;

export function OverviewTab({ task, onRun, running }: { task: TaskDetail; onRun: () => void; running?: boolean }) {
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const verify = useQuery({ queryKey: ["verify", task.id], queryFn: () => api.verify(task.id) });
  const snapshots = useQuery({ queryKey: ["snapshots", task.id], queryFn: () => api.snapshots(task.id) });
  const run = useQuery({
    queryKey: ["run", task.id],
    queryFn: () => api.runStatus(task.id),
    refetchInterval: (query) =>
      query.state.data?.state === "running" || query.state.data?.state === "queued" ? 2000 : 10_000,
  });
  const [interactiveCommand, setInteractiveCommand] = useState("");
  const [containerHint, setContainerHint] = useState("");
  const [showHeartbeat, setShowHeartbeat] = useState(false);
  const stop = useMutation({
    mutationFn: () => api.stopRun(task.id),
    onSuccess: () => {
      toast("已请求停止运行", "warn");
      void queryClient.invalidateQueries();
    },
    onError: (error) => toast(errorText(error), "error"),
  });
  const interactive = useMutation({
    mutationFn: () => api.interactive(task.id),
    onSuccess: (result) => {
      setInteractiveCommand(result.command);
      setContainerHint(result.container_hint);
      navigator.clipboard.writeText(result.command).then(
        () => toast("启动命令已复制，粘贴到终端执行", "info"),
        () => toast("启动命令已生成，请从下方手动复制", "warn"),
      );
    },
    onError: (error) => toast(errorText(error), "error"),
  });
  const collect = useMutation({
    mutationFn: () => api.collect(task.id, containerHint || null),
    onSuccess: (result) => {
      toast(result.ok ? "交互轨迹已收集" : result.errors[0] ?? "收集未通过", result.ok ? "ok" : "error");
      void queryClient.invalidateQueries();
    },
    onError: (error) => toast(errorText(error), "error"),
  });

  const state = run.data?.state;
  const active = state === "running" || state === "queued";
  const elapsed = run.data?.elapsed_s ?? 0;
  const overtime = active && elapsed > 90 * 60;
  const log = useMemo(() => {
    const lines = (run.data?.log_tail ?? "").split("\n");
    const kept = lines.filter((line) => !HEARTBEAT.test(line));
    return { all: lines.join("\n"), quiet: kept.join("\n").trim(), folded: lines.length - kept.length };
  }, [run.data?.log_tail]);

  return (
    <div className="tab-stack">
      <Panel title="交付状态">
        <div className="overview-banner">
          <StatusDot state={task.job_status} label={statusLabel(task.job_status)} />
          <span>{task.complete ? "11 项已全部通过，可以交付。" : task.missing[0] ?? "等待下一步。"}</span>
          {task.missing.length > 1 ? <Badge tone="warn">还差 {task.missing.length} 项</Badge> : null}
        </div>
        {task.missing.length > 1 ? (
          <ol className="missing-list">
            {task.missing.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ol>
        ) : null}
      </Panel>

      <Panel title="运行控制">
        <div className="run-overview">
          <div className="run-facts">
            {run.data ? (
              <span className="mono">
                第 {run.data.attempt_seq || 0}/{run.data.attempt_total || 3} 次 · {run.data.phase || "idle"}
              </span>
            ) : (
              <span className="mono">读取运行状态…</span>
            )}
            {elapsed > 0 ? (
              <span className={`mono ${overtime ? "warn-text" : ""}`}>
                {active ? "已用" : "用时"} {formatDuration(elapsed)}
              </span>
            ) : null}
            {run.data?.queue_position ? <Badge tone="info">队位 {run.data.queue_position}</Badge> : null}
            {overtime ? <Badge tone="warn">超过 90 分钟，仍在运行</Badge> : null}
          </div>
          <div className="form-actions">
            {active ? (
              <Button variant="danger" onClick={() => stop.mutate()} loading={stop.isPending}>
                <StopCircle size={14} /> 停止
              </Button>
            ) : (
              <Button
                variant="primary"
                data-primary
                disabled={task.job_status !== "ready"}
                loading={running}
                title={task.job_status === "ready" ? undefined : `当前状态是「${statusLabel(task.job_status)}」，不能开始运行`}
                onClick={onRun}
              >
                <Play size={14} /> 开始运行
              </Button>
            )}
            <Button
              disabled={task.job_status !== "ready"}
              onClick={() => interactive.mutate()}
              loading={interactive.isPending}
            >
              交互启动
            </Button>
            {interactiveCommand ? (
              <Button onClick={() => collect.mutate()} loading={collect.isPending}>
                已结束，收集
              </Button>
            ) : null}
          </div>
        </div>
        {run.isError ? <ErrorPanel error={run.error} title="运行状态读取失败" onRetry={() => void run.refetch()} /> : null}
        {task.skill_status === "run_failed" ? (
          <div className="gate-warning">
            运行失败。结构中断按规范恢复初始快照后重新 roll；连续 3 次仍失败应换题。原始错误见下方运行日志。
          </div>
        ) : null}
        {collect.data && !collect.data.ok ? (
          <div className="error-panel" role="alert">
            <AlertTriangle size={16} />
            <div>
              <strong>收集未通过</strong>
              <ul>{collect.data.errors.map((error) => <li key={error}>{error}</li>)}</ul>
              {collect.data.stderr ? <pre>{collect.data.stderr}</pre> : null}
            </div>
          </div>
        ) : null}
        {interactiveCommand ? (
          <CodeBlock title="交互启动命令（已脱敏）">{interactiveCommand}</CodeBlock>
        ) : null}
        {run.data?.log_tail ? (
          <CodeBlock
            title="运行日志尾部 · 技能 stderr / 容器 stdout"
            maxHeight={active ? 420 : 260}
            tail
            actions={
              log.folded ? (
                <Button size="small" variant="ghost" onClick={() => setShowHeartbeat((value) => !value)}>
                  {showHeartbeat ? "折叠心跳" : `已折叠 ${log.folded} 行心跳`}
                </Button>
              ) : null
            }
          >
            {showHeartbeat ? log.all : log.quiet || "（只有心跳行，容器仍在运行）"}
          </CodeBlock>
        ) : null}
      </Panel>

      <div className="two-column">
        <Panel title="任务事实">
          <div className="key-value-list">
            <KeyValue label="任务目录" value={task.task_root} mono copy={task.task_root} />
            <KeyValue label="所属领域" value={task.domain} />
            <KeyValue label="子方向" value={task.sub_direction} />
            <KeyValue label="任务类型" value={task.task_type} />
            <KeyValue label="难度" value={task.difficulty} />
            <KeyValue label="语言/框架" value={task.languages} />
          </div>
        </Panel>
        <Panel title="快照链路">
          {snapshots.isPending ? <LoadingLines /> : null}
          {snapshots.isError ? <ErrorPanel error={snapshots.error} onRetry={() => void snapshots.refetch()} /> : null}
          {snapshots.data ? (
            <div className="key-value-list">
              <KeyValue label="初始" value={snapshots.data.initial.sha} mono copy={snapshots.data.initial.sha} />
              <KeyValue label="产物" value={snapshots.data.artifact.sha} mono copy={snapshots.data.artifact.sha} />
              <KeyValue
                label="祖先关系"
                value={snapshots.data.ancestor_ok ? <Badge tone="ok">通过</Badge> : <Badge tone="danger">未通过</Badge>}
              />
              <KeyValue label="Diff" value={shortDetail(snapshots.data.artifact.short_stat)} mono />
            </div>
          ) : null}
        </Panel>
      </div>

      <Panel title="结构验证">
        {verify.isPending ? <LoadingLines /> : null}
        {verify.isError ? <ErrorPanel error={verify.error} onRetry={() => void verify.refetch()} /> : null}
        {verify.data ? (
          <CodeBlock title="trace.analyze" maxHeight={360}>{JSON.stringify(verify.data, null, 2)}</CodeBlock>
        ) : verify.isPending || verify.isError ? null : (
          <EmptyState title="运行完成后可读取真人 user 消息、最终 stop_reason 与 SessionID 唯一性。" />
        )}
      </Panel>
    </div>
  );
}

export function PromptTab({ task }: { task: TaskDetail }) {
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const theme = useUIStore((state) => state.theme);
  const prompt = useQuery({ queryKey: ["prompt", task.id], queryFn: () => api.prompt(task.id) });
  const reviews = useQuery({ queryKey: ["reviews", task.id], queryFn: () => api.reviews(task.id) });
  const { text, setText, dirty } = useDraft(prompt.data?.draft);
  const difficultyDraft = useDraft(reviews.data ? JSON.stringify(reviews.data.difficulty, null, 2) : undefined);
  const raDraft = useDraft(reviews.data ? JSON.stringify(reviews.data.ra_renhua, null, 2) : undefined);
  const [preview, setPreview] = useState<PromptPreview | null>(null);
  const [previewError, setPreviewError] = useState("");
  const [taskType, setTaskType] = useState(task.task_type ?? "功能开发");
  const [difficulty, setDifficulty] = useState<"困难" | "地狱">(
    task.difficulty === "地狱" ? "地狱" : "困难",
  );
  const [languages, setLanguages] = useState(task.languages ?? "");
  const [repro, setRepro] = useState(task.repro_level ?? "已容器化，可一键起环境");
  const [allowOver, setAllowOver] = useState(false);
  const [reopenOpen, setReopenOpen] = useState(false);
  const [reopenReason, setReopenReason] = useState("");

  useEffect(() => {
    if (!text) {
      setPreview(null);
      return;
    }
    let stale = false;
    const timer = window.setTimeout(() => {
      api.previewPrompt(task.id, text).then(
        (result) => {
          if (stale) return;
          setPreview(result);
          setPreviewError("");
        },
        (error: unknown) => {
          if (stale) return;
          setPreview(null);
          setPreviewError(errorText(error));
        },
      );
    }, 300);
    return () => {
      stale = true;
      window.clearTimeout(timer);
    };
  }, [task.id, text]);

  const save = useMutation({
    mutationFn: () => api.savePrompt(task.id, text),
    onSuccess: (result) => {
      setPreview(result);
      toast("题面草稿已保存", "ok");
      void queryClient.invalidateQueries({ queryKey: ["prompt", task.id] });
    },
    onError: (error) => toast(errorText(error), "error"),
  });
  const saveReviews = useMutation({
    mutationFn: async () => {
      const difficultyData = parseJsonObject(difficultyDraft.text, "难度论证");
      const raData = parseJsonObject(raDraft.text, "ra-人话审核");
      await Promise.all([
        api.saveReview(task.id, "难度论证", difficultyData),
        api.saveReview(task.id, "ra-人话审核", raData),
      ]);
    },
    onSuccess: () => {
      toast("两份审核记录已保存", "ok");
      void queryClient.invalidateQueries({ queryKey: ["reviews", task.id] });
    },
    onError: (error) => toast(errorText(error), "error"),
  });
  const install = useMutation({
    mutationFn: async () => {
      // 技能安装的是磁盘上的草稿，先把编辑器里的内容落盘，避免装上旧稿
      if (dirty) await api.savePrompt(task.id, text);
      return api.installPrompt(task.id, {
        task_type: taskType,
        difficulty,
        languages,
        repro_level: repro || null,
        domain: task.domain || "数据工程",
        sub_direction: task.sub_direction || "数仓与数据湖",
        allow_over_170: allowOver,
      });
    },
    onSuccess: (result) => {
      if (result.ok) toast("题面已通过技能门禁并安装", "ok");
      else toast(result.errors[0] ?? "题面门禁未通过", "error");
      void queryClient.invalidateQueries({ queryKey: ["task", task.id] });
      void queryClient.invalidateQueries({ queryKey: ["prompt", task.id] });
    },
    onError: (error) => toast(errorText(error), "error"),
  });
  const repo = useMutation({
    mutationFn: () => api.repo(task.id),
    onSuccess: () => {
      toast("快照已推送到 GitHub", "ok");
      void queryClient.invalidateQueries({ queryKey: ["task", task.id] });
    },
    onError: (error) => toast(errorText(error), "error"),
  });
  const reopen = useMutation({
    mutationFn: (reason: string) => api.reopenPrompt(task.id, reason),
    onSuccess: () => {
      toast("题面已解锁，可修改后重新安装", "warn");
      setReopenOpen(false);
      setReopenReason("");
      void queryClient.invalidateQueries({ queryKey: ["prompt", task.id] });
      void queryClient.invalidateQueries({ queryKey: ["task", task.id] });
    },
    onError: (error) => toast(errorText(error), "error"),
  });

  if (prompt.isPending) return <LoadingLines rows={7} />;
  if (prompt.isError) return <ErrorPanel error={prompt.error} onRetry={() => void prompt.refetch()} />;
  if (!prompt.data) return null;
  const readonly = prompt.data.readonly;
  const chars = countText(text);
  const soft = prompt.data.limits?.preferred ?? 170;
  const hard = prompt.data.limits?.hard ?? 240;
  const blockedBy = readonly
    ? "题面已安装并只读"
    : !text.trim()
      ? "题面为空"
      : chars > hard
        ? `超过 ${hard} 字`
        : !preview?.ok
          ? "离线预检未通过"
          : "";

  return (
    <div className="tab-stack">
      <div className="editor-frame">
        <div className="editor-toolbar">
          <div>
            <span className="eyebrow">PROMPT / ≤{hard} 字</span>
            <strong>
              {readonly ? "已安装，题面只读" : "题面草稿"}
              {dirty && !readonly ? <span className="unsaved"> · 有未保存的修改</span> : null}
            </strong>
          </div>
          <div className="meter-inline">
            <Meter value={chars} max={hard} warnAt={soft} label={`非空字 · 建议 ≤${soft}`} />
          </div>
          <div className="form-actions">
            <Button data-save disabled={readonly || !dirty} onClick={() => save.mutate()} loading={save.isPending}>
              <Save size={14} /> 保存
            </Button>
            {readonly && prompt.data.can_reopen ? (
              <Button variant="danger" onClick={() => setReopenOpen(true)}>
                重开本题
              </Button>
            ) : null}
          </div>
        </div>
        <div className="prompt-editor">
          <CodeMirror
            value={text}
            height="390px"
            theme={theme}
            readOnly={readonly}
            extensions={editorExtensions}
            onChange={setText}
            basicSetup={{ lineNumbers: true, foldGutter: false, searchKeymap: true }}
          />
          <aside className="gate-column">
            <h3>门禁预检</h3>
            <div className="gate-counter">
              <strong className={chars > hard ? "danger-text" : chars > soft ? "warn-text" : ""}>{chars}</strong>
              <span>/ {hard} 字</span>
            </div>
            {previewError ? <p className="gate-error">预检请求失败：{previewError}</p> : null}
            {preview?.errors.map((error) => <p key={error} className="gate-error">{error}</p>)}
            {preview?.warnings.map((warning) => <p key={warning} className="gate-warning">{warning}</p>)}
            {preview?.ok ? <p className="gate-ok"><Check size={13} /> 离线规则通过；安装时仍以技能为准</p> : null}
            {!text ? <p className="gate-warning">题面为空。</p> : null}
            <label className="check-line">
              <input
                type="checkbox"
                checked={allowOver}
                disabled={readonly}
                onChange={(event) => setAllowOver(event.target.checked)}
              />
              确有需要，允许超过 {soft} 字
            </label>
          </aside>
        </div>
      </div>

      <Panel title="安装参数" className="install-panel">
        <div className="form-grid">
          <Field label="任务类型">
            <select value={taskType} disabled={readonly} onChange={(event) => setTaskType(event.target.value)}>
              {taskTypes.map((type) => <option key={type}>{type}</option>)}
            </select>
          </Field>
          <Field label="难度">
            <select
              value={difficulty}
              disabled={readonly}
              onChange={(event) => setDifficulty(event.target.value as "困难" | "地狱")}
            >
              <option>困难</option>
              <option>地狱</option>
            </select>
          </Field>
          <Field label="语言/框架">
            <input value={languages} disabled={readonly} onChange={(event) => setLanguages(event.target.value)} />
          </Field>
          <Field label="复现等级">
            <select value={repro} disabled={readonly} onChange={(event) => setRepro(event.target.value)}>
              <option>无外部依赖</option>
              <option>有外部依赖，未容器化</option>
              <option>已容器化，可一键起环境</option>
            </select>
          </Field>
        </div>
        <div className="form-actions">
          <Button
            variant="primary"
            data-primary
            disabled={Boolean(blockedBy)}
            title={blockedBy || undefined}
            onClick={() => install.mutate()}
            loading={install.isPending}
          >
            安装题面
          </Button>
          <Button onClick={() => repo.mutate()} loading={repo.isPending}>
            <Upload size={14} /> 推送快照
          </Button>
          {blockedBy && !readonly ? <small>不能安装：{blockedBy}</small> : null}
        </div>
        {install.data && !install.data.ok ? (
          <div className="error-panel" role="alert">
            <AlertTriangle size={16} />
            <div>
              <strong>技能门禁未通过</strong>
              <ul>{install.data.errors.map((error) => <li key={error}>{error}</li>)}</ul>
              {install.data.stderr ? <pre>{install.data.stderr}</pre> : null}
            </div>
          </div>
        ) : null}
        {repo.isError ? <ErrorPanel error={repo.error} title="快照推送失败" onRetry={() => repo.mutate()} /> : null}
      </Panel>

      <div className="review-json-grid">
        <Panel title="难度论证 JSON">
          <textarea
            className="json-editor mono"
            spellCheck={false}
            value={difficultyDraft.text}
            onChange={(event) => difficultyDraft.setText(event.target.value)}
          />
        </Panel>
        <Panel
          title="ra-人话审核 JSON"
          actions={
            <Button
              size="small"
              disabled={!difficultyDraft.dirty && !raDraft.dirty}
              onClick={() => saveReviews.mutate()}
              loading={saveReviews.isPending}
            >
              保存两份审核记录
            </Button>
          }
        >
          <textarea
            className="json-editor mono"
            spellCheck={false}
            value={raDraft.text}
            onChange={(event) => raDraft.setText(event.target.value)}
          />
        </Panel>
      </div>

      <Modal
        open={reopenOpen}
        title="重开本题"
        onClose={() => setReopenOpen(false)}
        footer={
          <>
            <Button onClick={() => setReopenOpen(false)}>取消</Button>
            <Button
              variant="danger"
              disabled={!reopenReason.trim()}
              loading={reopen.isPending}
              onClick={() => reopen.mutate(reopenReason.trim())}
            >
              解锁题面
            </Button>
          </>
        }
      >
        <Field label="原因" hint="必填。原因会写入任务事件，供复核时追溯。">
          <textarea
            value={reopenReason}
            onChange={(event) => setReopenReason(event.target.value)}
            placeholder="为什么要改已安装的题面"
            rows={3}
          />
        </Field>
      </Modal>
    </div>
  );
}

const TIMELINE_ROW = 41;

export function TimelineTab({ task }: { task: TaskDetail }) {
  const [query, setQuery] = useState("");
  const [failedOnly, setFailedOnly] = useState(false);
  const [toolFilter, setToolFilter] = useState("");
  const [expanded, setExpanded] = useState<number | null>(null);
  // 收起动画播完之前，步骤正文还得留在 DOM 里
  const [mounted, setMounted] = useState<number | null>(null);
  const parentRef = useRef<HTMLDivElement>(null);
  const lockedUntil = useRef(0);
  const timeline = useQuery({ queryKey: ["timeline", task.id], queryFn: () => api.timeline(task.id) });
  const rows = useMemo(() => {
    const all = timeline.data?.steps ?? [];
    const term = query.trim().toLowerCase();
    return all.filter((step) => {
      const matches = !term || `${step.tool} ${step.summary}`.toLowerCase().includes(term);
      return matches && (!failedOnly || step.status === "failed") && (!toolFilter || step.tool === toolFilter);
    });
  }, [failedOnly, query, timeline.data?.steps, toolFilter]);
  const tools = useMemo(
    () => [...new Set((timeline.data?.steps ?? []).map((step) => step.tool))].sort(),
    [timeline.data?.steps],
  );
  const virtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => TIMELINE_ROW,
    getItemKey: (index) => rows[index].no,
    overscan: 8,
  });

  useEffect(() => {
    if (expanded !== null) {
      setMounted(expanded);
      return;
    }
    const timer = window.setTimeout(() => setMounted(null), 220);
    return () => window.clearTimeout(timer);
  }, [expanded]);

  const toggle = (row: TimelineStep, index: number) => {
    // 200ms 内的连点会让手风琴来回抖，直接丢弃
    const now = performance.now();
    if (now < lockedUntil.current) return;
    lockedUntil.current = now + 200;
    const opening = expanded !== row.no;
    setExpanded(opening ? row.no : null);
    if (opening) window.setTimeout(() => virtualizer.scrollToIndex(index, { align: "start" }), 210);
  };

  if (timeline.isPending) return <LoadingLines rows={8} />;
  if (timeline.isError) return <ErrorPanel error={timeline.error} onRetry={() => void timeline.refetch()} />;
  if (!timeline.data?.steps.length) return <EmptyState title="运行完成后生成轨迹时间线。" />;

  return (
    <div className="timeline-tab">
      <div className="timeline-toolbar">
        <label className="search-box">
          <Search size={14} />
          <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索工具或命令" />
        </label>
        <Button
          size="small"
          variant={failedOnly ? "danger" : "secondary"}
          aria-pressed={failedOnly}
          disabled={!timeline.data.counts.failed && !failedOnly}
          onClick={() => setFailedOnly((value) => !value)}
        >
          只看失败 · {timeline.data.counts.failed}
        </Button>
        <select value={toolFilter} onChange={(event) => setToolFilter(event.target.value)} aria-label="按工具筛选">
          <option value="">全部工具</option>
          {tools.map((tool) => <option key={tool} value={tool}>{tool}</option>)}
        </select>
        <span className="toolbar-count">
          {rows.length === timeline.data.counts.total ? "" : `${rows.length} / `}
          {timeline.data.counts.total} 步
        </span>
      </div>
      <div className="timeline-list" ref={parentRef}>
        {rows.length ? (
          <div style={{ height: `${virtualizer.getTotalSize()}px`, position: "relative" }}>
            {virtualizer.getVirtualItems().map((virtualRow) => {
              const row = rows[virtualRow.index];
              const open = expanded === row.no;
              return (
                <div
                  key={row.no}
                  ref={virtualizer.measureElement}
                  data-index={virtualRow.index}
                  className={`timeline-item ${open ? "is-open" : ""}`}
                  style={{ transform: `translateY(${virtualRow.start}px)` }}
                >
                  <button
                    type="button"
                    className={`timeline-row ${row.status === "failed" ? "is-failed" : ""}`}
                    aria-expanded={open}
                    onClick={() => toggle(row, virtualRow.index)}
                  >
                    <ChevronRight size={12} aria-hidden="true" />
                    <span className="timeline-no mono">#{row.no}</span>
                    <Badge tone={row.status === "failed" ? "danger" : "idle"}>{row.tool}</Badge>
                    <span className="timeline-summary" title={row.summary}>{row.summary}</span>
                    <time>{formatClock(row.ts)}</time>
                  </button>
                  <div className="step-body">
                    <div>{open || mounted === row.no ? <TimelineBody taskId={task.id} no={row.no} /> : null}</div>
                  </div>
                </div>
              );
            })}
          </div>
        ) : (
          <EmptyState title="没有匹配的步骤。" />
        )}
      </div>
    </div>
  );
}

function TimelineBody({ taskId, no }: { taskId: string; no: number }) {
  const detail = useQuery({
    queryKey: ["timeline-body", taskId, no],
    queryFn: () => api.timelineBody(taskId, no),
    staleTime: Infinity,
  });
  return (
    <div className="step-body-inner">
      {detail.isPending ? <LoadingLines rows={2} /> : null}
      {detail.isError ? <ErrorPanel error={detail.error} onRetry={() => void detail.refetch()} /> : null}
      {detail.data ? (
        <>
          <CodeBlock title="input" maxHeight={240}>{JSON.stringify(detail.data.input, null, 2)}</CodeBlock>
          <CodeBlock title={detail.data.is_error ? "output · 出错" : "output"} maxHeight={320}>
            {detail.data.output || "(无输出)"}
          </CodeBlock>
        </>
      ) : null}
    </div>
  );
}

export function ArtifactsTab({ task }: { task: TaskDetail }) {
  const { toast } = useToast();
  const diff = useQuery({ queryKey: ["diff", task.id], queryFn: () => api.diff(task.id) });
  const verify = useQuery({ queryKey: ["verify", task.id], queryFn: () => api.verify(task.id) });
  const [picked, setPicked] = useState("");
  const [command, setCommand] = useState("");
  // 没点过就默认看第一个文件，省一次点击
  const selected = picked || diff.data?.files[0]?.path || "";
  const file = useQuery({
    queryKey: ["diff-file", task.id, selected],
    queryFn: () => api.diffFile(task.id, selected),
    enabled: Boolean(selected),
  });
  const runVerify = useMutation({
    mutationFn: () => api.runVerify(task.id, command),
    onSuccess: () => {
      toast("复核结果已落盘", "ok");
      void verify.refetch();
    },
    onError: (error) => toast(errorText(error), "error"),
  });

  return (
    <div className="artifact-layout">
      <Panel title="变更文件" className="diff-files">
        {diff.isPending ? <LoadingLines rows={8} /> : null}
        {diff.isError ? <ErrorPanel error={diff.error} onRetry={() => void diff.refetch()} /> : null}
        {diff.data ? (
          <>
            <div className="diff-total mono">
              {diff.data.total.files} 个文件 · +{diff.data.total.add} −{diff.data.total.del}
            </div>
            <div className="diff-file-list">
              {diff.data.files.map((item) => {
                const path = splitPath(item.path);
                return (
                  <button
                    type="button"
                    key={item.path}
                    title={item.path}
                    className={selected === item.path ? "is-active" : ""}
                    onClick={() => setPicked(item.path)}
                  >
                    <span className={`file-status file-${item.status}`}>{statusLetter(item.status)}</span>
                    <span className="diff-path">
                      <span>{path.dir}</span>
                      <strong>{path.base}</strong>
                    </span>
                    <small className="mono">+{item.add} −{item.del}</small>
                  </button>
                );
              })}
            </div>
            {!diff.data.files.length ? <EmptyState title="产物相对初始快照没有变更。" /> : null}
          </>
        ) : null}
      </Panel>
      <div className="diff-main">
        {selected ? (
          <Panel title={selected} className="diff-panel">
            {file.isPending ? <LoadingLines rows={10} /> : null}
            {file.isError ? <ErrorPanel error={file.error} onRetry={() => void file.refetch()} /> : null}
            {file.data !== undefined ? <DiffView diff={file.data} /> : null}
          </Panel>
        ) : diff.isPending ? null : (
          <EmptyState icon={<FileCode2 size={28} />} title="运行产出变更后，在这里查看 unified diff。" />
        )}
        <Panel title="本机复核">
          <form
            className="verify-runner"
            onSubmit={(event) => {
              event.preventDefault();
              if (command.trim() && !runVerify.isPending) runVerify.mutate();
            }}
          >
            <input
              value={command}
              onChange={(event) => setCommand(event.target.value)}
              placeholder="例如 pytest -q"
              aria-label="复核命令"
              className="mono"
            />
            <Button type="submit" data-primary disabled={!command.trim()} loading={runVerify.isPending}>
              执行并留证
            </Button>
          </form>
          <p className="form-note">命令在 workspace 内执行，超时 300 秒，输出最多保留 64KB。</p>
          {verify.data?.runtime && Object.keys(verify.data.runtime).length ? (
            <CodeBlock title="runtime-verification.json" maxHeight={360}>
              {JSON.stringify(verify.data.runtime, null, 2)}
            </CodeBlock>
          ) : null}
        </Panel>
      </div>
    </div>
  );
}

export function RecordingTab({ task }: { task: TaskDetail }) {
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const plan = useQuery({ queryKey: ["record-plan", task.id], queryFn: () => api.recordPlan(task.id) });
  const windows = useQuery({ queryKey: ["windows"], queryFn: api.windows, refetchInterval: 10_000 });
  const recording = useQuery({ queryKey: ["recording-status"], queryFn: api.recordingStatus, refetchInterval: 1000 });
  const verify = useQuery({ queryKey: ["recording-verify", task.id], queryFn: () => api.verifyRecording(task.id) });
  const [pickedWindow, setPickedWindow] = useState(0);
  const planDraft = useDraft(plan.data ? JSON.stringify(plan.data.plan, null, 2) : undefined);
  const [chromeUrl, setChromeUrl] = useState("http://127.0.0.1:3000");
  const [overwrite, setOverwrite] = useState<"terminal" | "web" | null>(null);
  const [deleteOpen, setDeleteOpen] = useState(false);

  // 录哪个窗口必须由人来选；之前选的窗口关掉了就回到「未选择」
  const windowId = windows.data?.windows.some((item) => item.windowId === pickedWindow) ? pickedWindow : 0;

  const savePlan = useMutation({
    mutationFn: () => api.saveRecordPlan(task.id, parseJsonObject(planDraft.text, "录屏计划")),
    onSuccess: (result) => {
      toast(result.checked.ok ? "录屏计划通过" : "录屏计划未通过", result.checked.ok ? "ok" : "error");
      void queryClient.invalidateQueries({ queryKey: ["record-plan", task.id] });
    },
    onError: (error) => toast(errorText(error), "error"),
  });
  const generatePlan = useMutation({
    mutationFn: ({ mode, force }: { mode: "terminal" | "web"; force: boolean }) =>
      api.generateRecordPlan(task.id, mode, force),
    onSuccess: (result) => {
      planDraft.setText(JSON.stringify(result.plan, null, 2));
      setOverwrite(null);
      toast("录屏计划模板已生成，请补全真实命令", "ok");
      void queryClient.invalidateQueries({ queryKey: ["record-plan", task.id] });
    },
    onError: (error) => {
      setOverwrite(null);
      toast(errorText(error), "error");
    },
  });
  const requestTemplate = (mode: "terminal" | "web") => {
    if (plan.data?.exists) setOverwrite(mode);
    else generatePlan.mutate({ mode, force: false });
  };
  const start = useMutation({
    mutationFn: () => api.startRecording(task.id, windowId),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["recording-status"] }),
    onError: (error) => toast(errorText(error), "error"),
  });
  const stop = useMutation({
    mutationFn: () => api.stopRecording(task.id),
    onSuccess: () => {
      toast("录屏已停止，正在读取校验结果", "ok");
      void queryClient.invalidateQueries();
    },
    onError: (error) => toast(errorText(error), "error"),
  });
  const openTerminal = useMutation({
    mutationFn: () => api.recordTerminal(task.id),
    onSuccess: async (result) => {
      await windows.refetch();
      // 这个窗口是用户刚为本题录屏专门打开的，直接选中
      if (result.window_id) setPickedWindow(result.window_id);
      toast("录屏终端已打开：不带本机账号和主机名，已进入 workspace", "ok");
    },
    onError: (error) => toast(errorText(error), "error"),
  });
  const chromeStart = useMutation({
    mutationFn: () => api.chromeStart(task.id, chromeUrl),
    onSuccess: () => {
      toast("独立 Chrome 已启动", "ok");
      void windows.refetch();
    },
    onError: (error) => toast(errorText(error), "error"),
  });
  const chromeStop = useMutation({
    mutationFn: () => api.chromeStop(task.id),
    onSuccess: () => toast("独立 Chrome 已关闭并清理 profile", "ok"),
    onError: (error) => toast(errorText(error), "error"),
  });
  const deleteRecording = useMutation({
    mutationFn: () => api.deleteRecording(task.id),
    onSuccess: () => {
      toast("录屏文件已删除，可以重录", "warn");
      setDeleteOpen(false);
      void queryClient.invalidateQueries();
    },
    onError: (error) => {
      setDeleteOpen(false);
      toast(errorText(error), "error");
    },
  });

  const status = recording.data;
  const activeHere = status?.task_id === task.id;
  const counting = Boolean(status?.pending && activeHere);
  const recordingHere = Boolean(status?.recording && activeHere);
  const busyElsewhere = Boolean((status?.recording || status?.pending) && !activeHere);
  const planMode = plan.data?.plan.mode;
  const mode = typeof planMode === "string" ? planMode : "terminal";
  const planOk = Boolean(plan.data?.checked.ok) && !planDraft.dirty;
  const remaining = status?.remaining_s ?? 0;
  const total = Math.round((status?.elapsed_s ?? 0) + remaining) || 90;
  const timerTone = remaining <= 5 ? "danger" : remaining <= 20 ? "warn" : undefined;
  const startHint = busyElsewhere
    ? ""
    : planDraft.dirty
      ? "录屏计划有未保存的修改，先「保存并检查计划」。"
      : !plan.data?.checked.ok
        ? "录屏计划还没通过检查。"
        : !windowId
          ? "从上面的列表里选一个要录制的窗口。"
          : "";
  const count = status?.countdown_remaining ?? 0;
  // 技能对「文件不存在」也返回 ok=false；那不是不合格录屏，没有东西可删
  const hasRecording = Boolean(verify.data && (verify.data.ok || verify.data.duration_s > 0 || verify.data.width > 0));

  return (
    <div className="recording-layout">
      <Panel
        title="录屏计划"
        actions={
          <div className="form-actions">
            <Button
              size="small"
              loading={generatePlan.isPending && generatePlan.variables?.mode === "terminal"}
              onClick={() => requestTemplate("terminal")}
            >
              生成 Terminal 模板
            </Button>
            <Button
              size="small"
              loading={generatePlan.isPending && generatePlan.variables?.mode === "web"}
              onClick={() => requestTemplate("web")}
            >
              生成 Web 模板
            </Button>
          </div>
        }
      >
        {plan.isPending ? <LoadingLines /> : null}
        {plan.isError ? <ErrorPanel error={plan.error} onRetry={() => void plan.refetch()} /> : null}
        <textarea
          className="json-editor mono"
          spellCheck={false}
          value={planDraft.text}
          onChange={(event) => planDraft.setText(event.target.value)}
        />
        <div className="form-actions">
          <Button onClick={() => savePlan.mutate()} loading={savePlan.isPending}>保存并检查计划</Button>
          {planDraft.dirty ? (
            <Badge tone="warn">有未保存的修改</Badge>
          ) : plan.data ? (
            <Badge tone={plan.data.checked.ok ? "ok" : "danger"}>
              {plan.data.checked.ok ? "计划通过" : plan.data.checked.errors?.join("；") || "计划未通过"}
            </Badge>
          ) : null}
        </div>
        {mode === "web" ? (
          <div className="chrome-controls">
            <Field label="本机 URL">
              <input value={chromeUrl} onChange={(event) => setChromeUrl(event.target.value)} />
            </Field>
            <Button onClick={() => chromeStart.mutate()} loading={chromeStart.isPending}>启动独立 Chrome</Button>
            <Button onClick={() => chromeStop.mutate()} loading={chromeStop.isPending}>关闭并校验清理</Button>
          </div>
        ) : null}
      </Panel>

      <Panel
        title={`选择录制窗口${windows.data ? ` · ${windows.data.windows.length}` : ""}`}
        actions={
          <div className="form-actions">
            <Button
              size="small"
              disabled={counting || recordingHere}
              onClick={() => openTerminal.mutate()}
              loading={openTerminal.isPending}
            >
              打开录屏终端
            </Button>
            <Button size="small" variant="ghost" onClick={() => void windows.refetch()} loading={windows.isFetching}>
              刷新窗口
            </Button>
          </div>
        }
      >
        {windows.isPending ? <LoadingLines rows={4} /> : null}
        {windows.isError ? <ErrorPanel error={windows.error} onRetry={() => void windows.refetch()} /> : null}
        {windows.data && !windows.data.windows.length ? <EmptyState title="没有可录制的窗口。" /> : null}
        <p className="form-note">
          Terminal 段请用「打开录屏终端」新开的窗口。日常终端的标题和提示符带本机账号与主机名，技能会拒绝录制；
          录制中打印了家目录绝对路径（如 pwd）的片段也会作废。
        </p>
        <div className="window-list" role="radiogroup" aria-label="录制窗口">
          {windows.data?.windows.map((item: WindowRow) => (
            <label key={item.windowId} className={windowId === item.windowId ? "is-selected" : ""}>
              <input
                type="radio"
                name="window"
                checked={windowId === item.windowId}
                disabled={counting || recordingHere}
                onChange={() => setPickedWindow(item.windowId)}
              />
              <span>
                <strong>{item.app}</strong>
                <small>{item.title || "(无标题)"}</small>
              </span>
              <span className="mono">{item.width}×{item.height}</span>
              <code>#{item.windowId}</code>
            </label>
          ))}
        </div>
        <div className="record-controls">
          {recordingHere ? (
            <>
              <div className="record-timer" data-tone={timerTone}>
                <span className="rec-flag" data-blink={remaining <= 10 || undefined}>
                  <i aria-hidden="true" /> 录制中{remaining <= 10 ? ` · 还剩 ${Math.max(0, Math.round(remaining))} 秒` : ""}
                </span>
                <strong className="mono">
                  {formatSeconds(status?.elapsed_s ?? 0)} / {formatSeconds(total)}
                </strong>
              </div>
              <Meter value={Math.round(status?.elapsed_s ?? 0)} max={total} tone={timerTone ?? "info"} />
              <Button variant="danger" data-primary onClick={() => stop.mutate()} loading={stop.isPending}>
                <Square size={14} /> 停止录制
              </Button>
            </>
          ) : (
            <>
              <Button
                variant="primary"
                data-primary
                disabled={!windowId || !planOk || busyElsewhere || counting}
                onClick={() => start.mutate()}
                loading={start.isPending}
              >
                <Play size={14} /> 开始录制
              </Button>
              {startHint ? <small>{startHint}</small> : null}
            </>
          )}
          {busyElsewhere ? (
            <div className="gate-warning">
              <Pause size={14} /> 另一个录制进行中：<code>{status?.task_id ?? "未知任务"}</code>，主机级锁未释放。
            </div>
          ) : null}
        </div>
      </Panel>

      <Panel title="录屏校验">
        {verify.isPending ? <LoadingLines /> : null}
        {verify.isError ? <ErrorPanel error={verify.error} onRetry={() => void verify.refetch()} /> : null}
        {verify.data && !hasRecording ? (
          <EmptyState
            icon={<MonitorPlay size={28} />}
            title={`还没有录屏。${verify.data.errors.join("；")}`}
          />
        ) : verify.data ? (
          <div className={`verify-result ${verify.data.ok ? "is-ok" : "is-bad"}`}>
            <div>
              {verify.data.ok ? <Check size={18} /> : <AlertTriangle size={18} />}
              <strong>{verify.data.ok ? "录屏通过" : "录屏未通过"}</strong>
            </div>
            <KeyValue label="分辨率" value={`${verify.data.width}×${verify.data.height}`} mono />
            <KeyValue label="时长" value={`${verify.data.duration_s}s`} mono />
            <KeyValue label="采集" value={verify.data.capture_kind} />
            <KeyValue label="指针策略" value={verify.data.cursor_ok ? "none" : "异常"} />
            {verify.data.errors.map((error) => <p key={error} className="gate-error">{error}</p>)}
            {!verify.data.ok ? (
              <Button variant="danger" onClick={() => setDeleteOpen(true)}>
                <StopCircle size={14} /> 删除不合格录屏并重录
              </Button>
            ) : null}
          </div>
        ) : verify.isPending || verify.isError ? null : (
          <EmptyState icon={<MonitorPlay size={28} />} title="录制结束后在这里显示原始证据校验。" />
        )}
      </Panel>

      {hasRecording ? (
        <Panel
          title="录屏预览"
          actions={
            <a
              className="button button-secondary button-small"
              href={`/api/tasks/${encodeURIComponent(task.id)}/recording/video?download=1`}
            >
              <Download size={13} /> 下载原片
            </a>
          }
        >
          <div className="recording-preview">
            <video
              controls
              playsInline
              preload="metadata"
              src={`/api/tasks/${encodeURIComponent(task.id)}/recording/video`}
            >
              当前浏览器不支持视频播放，请下载原片查看。
            </video>
          </div>
        </Panel>
      ) : null}

      {counting ? (
        <div className="countdown-layer" role="alertdialog" aria-label="录制倒计时" data-last={count <= 1 || undefined}>
          <div className="countdown">
            {/* key 随秒数变化，圆环每秒重新走一圈 */}
            <svg viewBox="0 0 36 36" key={count} aria-hidden="true">
              <circle cx="18" cy="18" r="15.9155" />
              <circle cx="18" cy="18" r="15.9155" pathLength="100" />
            </svg>
            <strong>{count}</strong>
          </div>
          <small>即将开始录制，请不要再切换前台窗口</small>
        </div>
      ) : null}

      <ConfirmModal
        open={deleteOpen}
        title="删除不合格录屏"
        confirmLabel="删除并重录"
        tone="danger"
        loading={deleteRecording.isPending}
        onConfirm={() => deleteRecording.mutate()}
        onClose={() => setDeleteOpen(false)}
      >
        <p>会删除本题的 运行录屏.mp4 及两份采集证据，删除后不能恢复。</p>
      </ConfirmModal>

      <ConfirmModal
        open={overwrite !== null}
        title="覆盖现有录屏计划"
        confirmLabel="用模板覆盖"
        tone="danger"
        loading={generatePlan.isPending}
        onConfirm={() => overwrite && generatePlan.mutate({ mode: overwrite, force: true })}
        onClose={() => setOverwrite(null)}
      >
        <p>本题已经有一份录屏计划。用 {overwrite === "web" ? "Web" : "Terminal"} 模板覆盖后，现有内容不能恢复。</p>
      </ConfirmModal>
    </div>
  );
}

export function FeedbackTab({ task }: { task: TaskDetail }) {
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const theme = useUIStore((state) => state.theme);
  const feedback = useQuery({ queryKey: ["feedback", task.id], queryFn: () => api.feedback(task.id) });
  const { text, setText, dirty } = useDraft(feedback.data?.text);
  const [checked, setChecked] = useState(false);

  const init = useMutation({
    mutationFn: () => api.initFeedback(task.id),
    onSuccess: () => void feedback.refetch(),
    onError: (error) => toast(errorText(error), "error"),
  });
  const saveAndCheck = useMutation({
    mutationFn: async () => {
      await api.saveFeedback(task.id, text);
      return api.checkFeedback(task.id);
    },
    onSuccess: (result) => {
      toast(result.ok ? "Feedback 检查通过" : result.errors[0] ?? "Feedback 未通过", result.ok ? "ok" : "error");
      void queryClient.invalidateQueries({ queryKey: ["feedback", task.id] });
      void queryClient.invalidateQueries({ queryKey: ["task", task.id] });
    },
    onError: (error) => toast(errorText(error), "error"),
  });
  const ack = useMutation({
    mutationFn: () => api.ackFeedback(task.id),
    onSuccess: () => {
      toast("人工复核确认已留痕", "ok");
      setChecked(false);
      void queryClient.invalidateQueries();
    },
    onError: (error) => toast(errorText(error), "error"),
  });

  const data = feedback.data;
  // 检查结果只对「磁盘上那一版」有效：本地改过或文件被外部动过，都得重新检查
  const checkValid = Boolean(data?.checked.ok) && !data?.checked.stale && !dirty;
  const liveChars = countText(text);
  return (
    <div className="feedback-layout">
      <div className="feedback-editor-panel">
        <div className="editor-toolbar">
          <div>
            <span className="eyebrow">FEEDBACK / 人写，程序只检查</span>
            <strong>
              证据化复盘
              {dirty ? <span className="unsaved"> · 有未保存的修改</span> : null}
            </strong>
          </div>
          {data && !data.text && !text ? (
            <Button onClick={() => init.mutate()} loading={init.isPending}>生成空白模板</Button>
          ) : null}
        </div>
        {feedback.isError ? <ErrorPanel error={feedback.error} onRetry={() => void feedback.refetch()} /> : null}
        <CodeMirror
          value={text}
          height="430px"
          theme={theme}
          extensions={editorExtensions}
          onChange={(value) => {
            setText(value);
            setChecked(false);
          }}
          basicSetup={{ lineNumbers: true, foldGutter: false }}
        />
        <div className="feedback-actions">
          <Button
            variant={checkValid ? "secondary" : "primary"}
            data-primary
            data-save
            onClick={() => saveAndCheck.mutate()}
            loading={saveAndCheck.isPending}
          >
            <ShieldCheck size={14} /> 保存并检查
          </Button>
          <label className="check-line">
            <input
              type="checkbox"
              checked={checked}
              disabled={!checkValid}
              onChange={(event) => setChecked(event.target.checked)}
            />
            我已逐条复核事实、分数与措辞
          </label>
          <Button
            variant={checkValid ? "primary" : "secondary"}
            disabled={!checked || !checkValid}
            onClick={() => ack.mutate()}
            loading={ack.isPending}
          >
            确认复核
          </Button>
        </div>
      </div>
      <aside className="feedback-meters">
        <Panel title="反馈指标">
          {feedback.isPending ? <LoadingLines rows={4} /> : null}
          {data ? (
            <>
              <Meter value={dirty ? liveChars : data.chars} max={360} warnAt={280} min={80} label="非空字 · 建议 180–280" />
              <Meter value={data.anchors} max={6} warnAt={3} label="证据锚点 · 建议 ≤3" />
              <Meter value={Math.min(100, Math.round(data.digits_ratio * 1000))} max={100} label="数字密度 ‰" tone="info" />
              {dirty ? <p className="meter-note">字数随输入更新；锚点等指标保存并检查后刷新。</p> : null}
              <div className="coverage-grid">
                <span className={data.coverage.process ? "covered" : ""}>过程</span>
                <span className={data.coverage.artifact ? "covered" : ""}>产物</span>
              </div>
              <div className="check-status">
                <StatusDot
                  state={checkValid ? "done" : data.checked.ok ? "reviewed" : "blocked"}
                  label={
                    data.checked.stale
                      ? "文件在外部被修改，检查结果已过期"
                      : dirty
                        ? "内容已修改，需要重新保存并检查"
                        : data.checked.ok
                          ? `检查通过 · ${data.checked.score ?? "—"} 分`
                          : "检查未通过"
                  }
                />
                {data.checked.errors.map((error) => <p key={error} className="gate-error">{error}</p>)}
                {data.checked.warnings.map((warning) => <p key={warning} className="gate-warning">{warning}</p>)}
              </div>
              <div className="ack-status">
                <StatusDot
                  state={data.review_ack.valid ? "done" : "idle"}
                  label={data.review_ack.valid ? "人工复核有效" : "尚未确认人工复核"}
                />
              </div>
            </>
          ) : null}
        </Panel>
      </aside>
    </div>
  );
}

export function ExportTab({ task }: { task: TaskDetail }) {
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const [result, setResult] = useState<ExportResult | null>(null);
  const [archiveOpen, setArchiveOpen] = useState(false);
  const resultFile = result?.file ?? "";
  const previewName = resultFile.split("/").pop() ?? "";
  const preview = useQuery({
    queryKey: ["export-preview", previewName],
    queryFn: () => api.exportPreview(previewName),
    enabled: Boolean(previewName),
  });
  const create = useMutation({
    mutationFn: () => api.export({ task_ids: [task.id] }),
    onSuccess: (next) => {
      setResult(next);
      if (next.file) toast(`已导出 ${next.rows} 行`, "ok");
      else toast(next.skipped[0]?.reason[0] ?? "任务未通过导出检查", "error");
      void queryClient.invalidateQueries();
    },
    onError: (error) => toast(errorText(error), "error"),
  });
  const archive = useMutation({
    mutationFn: () => api.archiveTask(task.id),
    onSuccess: (next) => {
      toast(next.release_error ? `已归档，但工程归还失败：${next.release_error}` : "已归档并归还工程", next.release_error ? "warn" : "ok");
      setArchiveOpen(false);
      void queryClient.invalidateQueries();
    },
    onError: (error) => {
      setArchiveOpen(false);
      toast(errorText(error), "error");
    },
  });
  const exported = task.job_status === "exported";

  return (
    <div className="tab-stack">
      <Panel title={`11 项完成度 · ${task.steps.filter((step) => step.ok).length}/11`}>
        <div className="gate-checklist">
          {task.steps.map((step: Step) => (
            <div key={step.number} className={step.ok ? "is-ok" : "is-pending"}>
              <span>{step.ok ? <Check size={12} /> : step.number}</span>
              <strong>{step.name}</strong>
              <small className="mono" title={step.detail}>{step.detail}</small>
            </div>
          ))}
        </div>
        {task.missing.length ? (
          <div className="gate-failures">
            <strong>当前缺项</strong>
            <ul>{task.missing.map((item) => <li key={item}>{item}</li>)}</ul>
          </div>
        ) : null}
      </Panel>

      <Panel title="交付表">
        <div className="export-actions">
          <Button
            variant={exported && !result ? "secondary" : "primary"}
            data-primary
            disabled={!task.complete}
            title={task.complete ? undefined : "11 项未全部通过，不能导出"}
            onClick={() => create.mutate()}
            loading={create.isPending}
          >
            <Upload size={14} /> {exported ? "重新生成 19 列 Excel" : "生成 19 列 Excel"}
          </Button>
          {resultFile ? (
            <>
              <a className="button button-secondary button-default" href={`/api/exports/${encodeURIComponent(previewName)}/download`}>
                <Download size={14} /> 下载
              </a>
              <Button
                variant="ghost"
                onClick={() => {
                  navigator.clipboard.writeText(resultFile).then(
                    () => toast("路径已复制", "info"),
                    (error: unknown) => toast(`复制失败：${errorText(error)}`, "error"),
                  );
                }}
              >
                <Copy size={14} /> 复制路径
              </Button>
            </>
          ) : null}
          <Button
            variant={exported && !result ? "primary" : "secondary"}
            disabled={!exported}
            title={exported ? undefined : "导出之后才能归档归还"}
            onClick={() => setArchiveOpen(true)}
          >
            归档并归还
          </Button>
        </div>
        {!task.complete ? <p className="form-note">还有 {task.missing.length} 项未通过，导出入口保持禁用。</p> : null}
        {resultFile ? <p className="mono path-line">{resultFile}</p> : null}
        {result && !result.file && result.skipped.length ? (
          <div className="gate-failures" role="alert">
            <strong>技能拒绝导出</strong>
            <ul>{result.skipped.flatMap((item) => item.reason).map((reason) => <li key={reason}>{reason}</li>)}</ul>
          </div>
        ) : null}
      </Panel>

      {previewName ? (
        <Panel title={`Excel 预览 · ${previewName}`}>
          {preview.isPending ? <LoadingLines rows={6} /> : null}
          {preview.isError ? <ErrorPanel error={preview.error} onRetry={() => void preview.refetch()} /> : null}
          {preview.data ? (
            <div className="table-scroll">
              <table className="data-table preview-table">
                <thead>
                  <tr>{preview.data.headers.map((header, index) => <th key={index}>{String(header ?? "—")}</th>)}</tr>
                </thead>
                <tbody>
                  {preview.data.rows.map((row, index) => (
                    <tr key={index}>
                      {row.map((cell, cellIndex) => (
                        <td key={cellIndex} title={String(cell ?? "")}>{String(cell ?? "—")}</td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : null}
        </Panel>
      ) : null}

      <ConfirmModal
        open={archiveOpen}
        title="归档并归还"
        confirmLabel="归档并归还"
        tone="danger"
        loading={archive.isPending}
        onConfirm={() => archive.mutate()}
        onClose={() => setArchiveOpen(false)}
      >
        <p>会先清理本题容器，再把工程归还给工程台。</p>
        <p>归档后不可撤销；本地任务目录与已导出的表格不会被删除。</p>
      </ConfirmModal>
    </div>
  );
}

function DiffView({ diff }: { diff: string }) {
  const lines = useMemo(() => diff.split("\n"), [diff]);
  if (!diff.trim()) return <EmptyState title="这个文件没有文本 diff（可能是二进制或仅改了权限）。" />;
  return (
    <pre className="diff-view" tabIndex={0}>
      {lines.map((line, index) => {
        const kind = line.startsWith("+") && !line.startsWith("+++") ? "add" : line.startsWith("-") && !line.startsWith("---") ? "del" : line.startsWith("@@") ? "hunk" : "";
        return <span key={index} className={`diff-line diff-${kind || "plain"}`}>{line || " "}</span>;
      })}
    </pre>
  );
}

function parseJsonObject(value: string, label: string): Record<string, unknown> {
  let parsed: unknown;
  try {
    parsed = JSON.parse(value);
  } catch (error) {
    throw new Error(`${label}不是合法 JSON：${errorText(error)}`, { cause: error });
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error(`${label}必须是 JSON 对象`);
  }
  return parsed as Record<string, unknown>;
}

function countText(value: string): number {
  return value.replace(/\s+/g, "").length;
}

function statusLetter(status: string): string {
  return status === "added" ? "A" : status === "deleted" ? "D" : status === "renamed" ? "R" : "M";
}

function formatSeconds(value: number): string {
  const seconds = Math.max(0, Math.round(value));
  return `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
}
