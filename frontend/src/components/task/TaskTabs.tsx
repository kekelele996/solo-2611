import { markdown } from "@codemirror/lang-markdown";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useVirtualizer } from "@tanstack/react-virtual";
import CodeMirror from "@uiw/react-codemirror";
import {
  AlertTriangle,
  Check,
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
import type { PromptPreview, Step, TaskDetail, WindowRow } from "../../api/types";
import {
  Badge,
  Button,
  CodeBlock,
  EmptyState,
  ErrorPanel,
  Field,
  KeyValue,
  LoadingLines,
  Meter,
  Panel,
  StatusDot,
} from "../ui";
import { useToast } from "../toast-context";
import { formatClock } from "../../utils/time";

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

export function OverviewTab({ task, onRun }: { task: TaskDetail; onRun: () => void }) {
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const verify = useQuery({ queryKey: ["verify", task.id], queryFn: () => api.verify(task.id) });
  const snapshots = useQuery({ queryKey: ["snapshots", task.id], queryFn: () => api.snapshots(task.id) });
  const run = useQuery({
    queryKey: ["run", task.id],
    queryFn: () => api.runStatus(task.id),
    refetchInterval: 2000,
  });
  const [interactiveCommand, setInteractiveCommand] = useState("");
  const [containerHint, setContainerHint] = useState("");
  const stop = useMutation({
    mutationFn: () => api.stopRun(task.id),
    onSuccess: () => {
      toast("已请求停止运行", "warn");
      void queryClient.invalidateQueries();
    },
    onError: (error) => toast(error instanceof Error ? error.message : String(error), "error"),
  });
  const interactive = useMutation({
    mutationFn: () => api.interactive(task.id),
    onSuccess: (result) => {
      setInteractiveCommand(result.command);
      setContainerHint(result.container_hint);
      void navigator.clipboard.writeText(result.command).then(() => toast("启动命令已复制", "info"));
    },
    onError: (error) => toast(error instanceof Error ? error.message : String(error), "error"),
  });
  const collect = useMutation({
    mutationFn: () => api.collect(task.id, containerHint || null),
    onSuccess: (result) => {
      toast(result.ok ? "交互轨迹已收集" : result.errors[0] ?? "收集未通过", result.ok ? "ok" : "error");
      void queryClient.invalidateQueries();
    },
    onError: (error) => toast(error instanceof Error ? error.message : String(error), "error"),
  });
  const elapsed = run.data?.elapsed_s ?? 0;
  return (
    <div className="tab-stack">
      <Panel title="交付状态">
        <div className="overview-banner">
          <StatusDot state={task.job_status} label={task.job_status} />
          <span>{task.complete ? "11 项已全部通过，可以交付。" : task.missing[0] ?? "等待下一步。"}</span>
          {!task.complete && task.job_status === "ready" ? (
            <Button variant="primary" data-primary onClick={onRun}>
              <Play size={14} /> 开始运行
            </Button>
          ) : null}
        </div>
        {task.missing.length ? (
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
            <StatusDot state={task.job_status} label={task.job_status} />
            <span className="mono">
              attempt {run.data?.attempt_seq || 0}/{run.data?.attempt_total || 3} · {run.data?.phase ?? "idle"}
            </span>
            <span className={`mono ${elapsed > 90 * 60 ? "warn-text" : ""}`}>
              已用 {formatSeconds(elapsed)}
            </span>
            {run.data?.queue_position ? <Badge tone="info">队位 {run.data.queue_position}</Badge> : null}
            {elapsed > 90 * 60 ? <Badge tone="warn">超过 90 分钟，仍在运行</Badge> : null}
          </div>
          <div className="form-actions">
            {run.data?.state === "running" || run.data?.state === "queued" ? (
              <Button variant="danger" onClick={() => stop.mutate()} loading={stop.isPending}>
                <StopCircle size={14} /> 停止
              </Button>
            ) : (
              <Button variant="primary" disabled={task.job_status !== "ready"} onClick={onRun}>
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
        {task.skill_status === "run_failed" ? (
          <div className="gate-warning">
            运行失败。结构中断按规范恢复初始快照后重新 roll；连续 3 次仍失败应换题。原始错误见任务事件与运行日志。
          </div>
        ) : null}
        {interactiveCommand ? (
          <CodeBlock title="交互启动命令（已脱敏）">{interactiveCommand}</CodeBlock>
        ) : null}
        {run.data?.log_tail ? <CodeBlock title="run stdout / stderr / container tail">{run.data.log_tail}</CodeBlock> : null}
      </Panel>

      <div className="two-column">
        <Panel title="任务事实">
          <div className="key-value-list">
            <KeyValue label="任务目录" value={task.task_root} mono />
            <KeyValue label="所属领域" value={task.domain} />
            <KeyValue label="子方向" value={task.sub_direction} />
            <KeyValue label="任务类型" value={task.task_type} />
            <KeyValue label="难度" value={task.difficulty} />
            <KeyValue label="语言/框架" value={task.languages} />
            <KeyValue label="初始快照" value={task.initial_sha} mono />
            <KeyValue label="产物快照" value={task.artifact_sha} mono />
          </div>
        </Panel>
        <Panel title="快照链路">
          {snapshots.isPending ? <LoadingLines /> : null}
          {snapshots.isError ? <ErrorPanel error={snapshots.error} /> : null}
          {snapshots.data ? (
            <div className="key-value-list">
              <KeyValue label="初始" value={shortSha(snapshots.data.initial.sha)} mono />
              <KeyValue label="产物" value={shortSha(snapshots.data.artifact.sha)} mono />
              <KeyValue
                label="祖先关系"
                value={snapshots.data.ancestor_ok ? <Badge tone="ok">通过</Badge> : <Badge tone="danger">未通过</Badge>}
              />
              <KeyValue label="Diff" value={snapshots.data.artifact.short_stat} />
            </div>
          ) : null}
        </Panel>
      </div>

      <Panel title="结构验证">
        {verify.isPending ? <LoadingLines /> : null}
        {verify.isError ? <ErrorPanel error={verify.error} /> : null}
        {verify.data ? (
          <CodeBlock title="trace.analyze">{JSON.stringify(verify.data, null, 2)}</CodeBlock>
        ) : (
          <EmptyState title="运行完成后可读取真人 user 消息、最终 stop_reason 与 SessionID 唯一性。" />
        )}
      </Panel>
    </div>
  );
}

export function PromptTab({ task }: { task: TaskDetail }) {
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const prompt = useQuery({ queryKey: ["prompt", task.id], queryFn: () => api.prompt(task.id) });
  const reviews = useQuery({ queryKey: ["reviews", task.id], queryFn: () => api.reviews(task.id) });
  const [text, setText] = useState("");
  const [preview, setPreview] = useState<PromptPreview | null>(null);
  const [taskType, setTaskType] = useState(task.task_type ?? "功能开发");
  const [difficulty, setDifficulty] = useState<"困难" | "地狱">(
    task.difficulty === "地狱" ? "地狱" : "困难",
  );
  const [languages, setLanguages] = useState(task.languages ?? "");
  const [repro, setRepro] = useState(task.repro_level ?? "已容器化，可一键起环境");
  const [allowOver, setAllowOver] = useState(false);
  const [difficultyJson, setDifficultyJson] = useState("{}");
  const [raJson, setRaJson] = useState("{}");

  useEffect(() => {
    if (prompt.data) setText(prompt.data.draft);
  }, [prompt.data]);
  useEffect(() => {
    if (reviews.data) {
      setDifficultyJson(JSON.stringify(reviews.data.difficulty, null, 2));
      setRaJson(JSON.stringify(reviews.data.ra_renhua, null, 2));
    }
  }, [reviews.data]);
  useEffect(() => {
    if (!text) return;
    const timer = window.setTimeout(() => {
      void api.previewPrompt(task.id, text).then(setPreview).catch(() => setPreview(null));
    }, 300);
    return () => window.clearTimeout(timer);
  }, [task.id, text]);

  const save = useMutation({
    mutationFn: () => api.savePrompt(task.id, text),
    onSuccess: (result) => {
      setPreview(result);
      toast("题面草稿已保存", "ok");
      void queryClient.invalidateQueries({ queryKey: ["prompt", task.id] });
    },
    onError: (error) => toast(error instanceof Error ? error.message : String(error), "error"),
  });
  const saveReviews = useMutation({
    mutationFn: async () => {
      const difficultyData = parseJsonObject(difficultyJson, "难度论证");
      const raData = parseJsonObject(raJson, "ra-人话审核");
      await Promise.all([
        api.saveReview(task.id, "难度论证", difficultyData),
        api.saveReview(task.id, "ra-人话审核", raData),
      ]);
    },
    onSuccess: () => toast("两份审核记录已保存", "ok"),
    onError: (error) => toast(error instanceof Error ? error.message : String(error), "error"),
  });
  const install = useMutation({
    mutationFn: () =>
      api.installPrompt(task.id, {
        task_type: taskType,
        difficulty,
        languages,
        repro_level: repro || null,
        domain: task.domain || "数据工程",
        sub_direction: task.sub_direction || "其他",
        allow_over_170: allowOver,
      }),
    onSuccess: (result) => {
      if (result.ok) {
        toast("题面已通过技能门禁并安装", "ok");
        void queryClient.invalidateQueries({ queryKey: ["task", task.id] });
      } else {
        toast(result.errors[0] ?? "题面门禁未通过", "error");
      }
    },
    onError: (error) => toast(error instanceof Error ? error.message : String(error), "error"),
  });
  const repo = useMutation({
    mutationFn: () => api.repo(task.id),
    onSuccess: () => {
      toast("快照已推送到 GitHub", "ok");
      void queryClient.invalidateQueries({ queryKey: ["task", task.id] });
    },
    onError: (error) => toast(error instanceof Error ? error.message : String(error), "error"),
  });
  const reopen = useMutation({
    mutationFn: (reason: string) => api.reopenPrompt(task.id, reason),
    onSuccess: () => {
      toast("题面已解锁，可修改后重新安装", "warn");
      void queryClient.invalidateQueries({ queryKey: ["prompt", task.id] });
    },
    onError: (error) => toast(error instanceof Error ? error.message : String(error), "error"),
  });

  if (prompt.isPending) return <LoadingLines rows={7} />;
  if (prompt.isError) return <ErrorPanel error={prompt.error} onRetry={() => void prompt.refetch()} />;
  if (!prompt.data) return null;
  const readonly = prompt.data.readonly;
  const chars = countText(text);

  return (
    <div className="tab-stack">
      <div className="editor-toolbar">
        <div>
          <span className="eyebrow">PROMPT / &lt;=240 字</span>
          <strong>{readonly ? "已运行，题面只读" : "题面草稿"}</strong>
        </div>
        <div className="meter-inline">
          <Meter value={chars} max={240} label="非空字" />
        </div>
        <Button data-save disabled={readonly} onClick={() => save.mutate()} loading={save.isPending}>
          <Save size={14} /> 保存
        </Button>
        {readonly && prompt.data.can_reopen ? (
          <Button
            variant="danger"
            onClick={() => {
              const reason = window.prompt("重开本题必须说明原因。为什么要改题面？");
              if (reason?.trim()) reopen.mutate(reason.trim());
            }}
            loading={reopen.isPending}
          >
            重开本题
          </Button>
        ) : null}
      </div>
      <div className="prompt-editor">
        <CodeMirror
          value={text}
          height="390px"
          readOnly={readonly}
          extensions={[markdown()]}
          onChange={setText}
          basicSetup={{ lineNumbers: true, foldGutter: true, searchKeymap: true }}
        />
        <aside className="gate-column">
          <h3>门禁预检</h3>
          <div className="gate-counter">
            <strong className={chars > 240 ? "danger-text" : chars > 170 ? "warn-text" : ""}>{chars}</strong>
            <span>/ 240 字</span>
          </div>
          {preview?.errors.map((error) => <p key={error} className="gate-error">{error}</p>)}
          {preview?.warnings.map((warning) => <p key={warning} className="gate-warning">{warning}</p>)}
          {preview?.ok ? <p className="gate-ok"><Check size={13} /> 离线规则通过；安装时仍以技能为准</p> : null}
          <label className="check-line">
            <input type="checkbox" checked={allowOver} onChange={(event) => setAllowOver(event.target.checked)} />
            确有需要，允许超过 170 字
          </label>
        </aside>
      </div>

      <Panel title="安装参数" className="install-panel">
        <div className="form-grid">
          <Field label="任务类型">
            <select value={taskType} onChange={(event) => setTaskType(event.target.value)}>
              {taskTypes.map((type) => <option key={type}>{type}</option>)}
            </select>
          </Field>
          <Field label="难度">
            <select value={difficulty} onChange={(event) => setDifficulty(event.target.value as "困难" | "地狱")}>
              <option>困难</option>
              <option>地狱</option>
            </select>
          </Field>
          <Field label="语言/框架">
            <input value={languages} onChange={(event) => setLanguages(event.target.value)} />
          </Field>
          <Field label="复现等级">
            <select value={repro} onChange={(event) => setRepro(event.target.value)}>
              <option>无外部依赖</option>
              <option>有外部依赖，未容器化</option>
              <option>已容器化，可一键起环境</option>
            </select>
          </Field>
        </div>
        <div className="form-actions">
          <Button onClick={() => saveReviews.mutate()} loading={saveReviews.isPending}>
            保存两份审核记录
          </Button>
          <Button
            variant="primary"
            data-primary
            disabled={readonly || !text.trim() || !preview?.ok}
            onClick={() => install.mutate()}
            loading={install.isPending}
          >
            安装题面
          </Button>
          <Button onClick={() => repo.mutate()} loading={repo.isPending}>
            <Upload size={14} /> 推送快照
          </Button>
        </div>
        {install.data && !install.data.ok ? (
          <div className="error-panel">
            <AlertTriangle size={16} />
            <div>
              <strong>技能门禁未通过</strong>
              <ul>{install.data.errors.map((error) => <li key={error}>{error}</li>)}</ul>
              <pre>{install.data.stderr}</pre>
            </div>
          </div>
        ) : null}
        {repo.isError ? <ErrorPanel error={repo.error} onRetry={() => repo.mutate()} /> : null}
      </Panel>

      <div className="review-json-grid">
        <Panel title="难度论证 JSON">
          <textarea
            className="json-editor mono"
            value={difficultyJson}
            onChange={(event) => setDifficultyJson(event.target.value)}
          />
        </Panel>
        <Panel title="ra-人话审核 JSON">
          <textarea
            className="json-editor mono"
            value={raJson}
            onChange={(event) => setRaJson(event.target.value)}
          />
        </Panel>
      </div>
    </div>
  );
}

export function TimelineTab({ task }: { task: TaskDetail }) {
  const [query, setQuery] = useState("");
  const [failedOnly, setFailedOnly] = useState(false);
  const [toolFilter, setToolFilter] = useState("");
  const [expanded, setExpanded] = useState<number | null>(null);
  const parentRef = useRef<HTMLDivElement>(null);
  const timeline = useQuery({ queryKey: ["timeline", task.id], queryFn: () => api.timeline(task.id) });
  const detail = useQuery({
    queryKey: ["timeline-body", task.id, expanded],
    queryFn: () => api.timelineBody(task.id, expanded ?? 0),
    enabled: expanded !== null,
  });
  const rows = useMemo(() => {
    const all = timeline.data?.steps ?? [];
    return all.filter((step) => {
      const matches = !query || `${step.tool} ${step.summary}`.toLowerCase().includes(query.toLowerCase());
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
    estimateSize: () => 46,
    overscan: 8,
  });

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
          onClick={() => setFailedOnly((value) => !value)}
        >
          只看失败 · {timeline.data.counts.failed}
        </Button>
        <select value={toolFilter} onChange={(event) => setToolFilter(event.target.value)} aria-label="按工具筛选">
          <option value="">全部工具</option>
          {tools.map((tool) => <option key={tool} value={tool}>{tool}</option>)}
        </select>
        <span className="toolbar-count">{timeline.data.counts.total} 步</span>
      </div>
      <div className="timeline-list" ref={parentRef}>
        <div style={{ height: `${virtualizer.getTotalSize()}px`, position: "relative" }}>
          {virtualizer.getVirtualItems().map((virtualRow) => {
            const row = rows[virtualRow.index];
            return (
              <button
                key={row.no}
                className={`timeline-row ${row.status === "failed" ? "is-failed" : ""}`}
                style={{ transform: `translateY(${virtualRow.start}px)` }}
                onClick={() => setExpanded(row.no)}
              >
                <span className="timeline-no mono">#{row.no}</span>
                <Badge tone={row.status === "failed" ? "danger" : "ok"}>{row.tool}</Badge>
                <span className="timeline-summary">{row.summary}</span>
                <time>{formatClock(row.ts)}</time>
              </button>
            );
          })}
        </div>
      </div>
      {expanded !== null ? (
        <Panel
          title={`步骤 #${expanded} 完整输入 / 输出`}
          actions={<Button size="small" variant="ghost" onClick={() => setExpanded(null)}>收起</Button>}
        >
          {detail.isPending ? <LoadingLines /> : null}
          {detail.data ? (
            <>
              <CodeBlock title="input">{JSON.stringify(detail.data.input, null, 2)}</CodeBlock>
              <CodeBlock title="output">{detail.data.output || "(无输出)"}</CodeBlock>
            </>
          ) : null}
        </Panel>
      ) : null}
    </div>
  );
}

export function ArtifactsTab({ task }: { task: TaskDetail }) {
  const { toast } = useToast();
  const diff = useQuery({ queryKey: ["diff", task.id], queryFn: () => api.diff(task.id) });
  const verify = useQuery({ queryKey: ["verify", task.id], queryFn: () => api.verify(task.id) });
  const [selected, setSelected] = useState("");
  const [command, setCommand] = useState("");
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
    onError: (error) => toast(error instanceof Error ? error.message : String(error), "error"),
  });

  return (
    <div className="artifact-layout">
      <Panel title="变更文件" className="diff-files">
        {diff.isPending ? <LoadingLines rows={8} /> : null}
        {diff.isError ? <ErrorPanel error={diff.error} /> : null}
        {diff.data ? (
          <>
            <div className="diff-total mono">
              {diff.data.total.files} files · +{diff.data.total.add} −{diff.data.total.del}
            </div>
            {diff.data.files.map((item) => (
              <button
                key={item.path}
                className={selected === item.path ? "is-active" : ""}
                onClick={() => setSelected(item.path)}
              >
                <span className={`file-status file-${item.status}`}>{statusLetter(item.status)}</span>
                <span className="ellipsis">{item.path}</span>
                <small className="mono">+{item.add} −{item.del}</small>
              </button>
            ))}
          </>
        ) : null}
      </Panel>
      <div className="diff-main">
        {selected ? (
          <Panel title={selected}>
            {file.isPending ? <LoadingLines rows={10} /> : null}
            {file.isError ? <ErrorPanel error={file.error} /> : null}
            {file.data ? <DiffView diff={file.data} /> : null}
          </Panel>
        ) : (
          <EmptyState icon={<FileCode2 size={28} />} title="从左侧选择一个文件查看 unified diff。" />
        )}
        <Panel title="本机复核">
          <div className="verify-runner">
            <input
              value={command}
              onChange={(event) => setCommand(event.target.value)}
              placeholder="例如 pytest -q"
              className="mono"
            />
            <Button
              data-primary
              disabled={!command.trim()}
              onClick={() => runVerify.mutate()}
              loading={runVerify.isPending}
            >
              执行并留证
            </Button>
          </div>
          {verify.data?.runtime && Object.keys(verify.data.runtime).length ? (
            <CodeBlock title="runtime-verification.json">{JSON.stringify(verify.data.runtime, null, 2)}</CodeBlock>
          ) : (
            <p className="form-note">命令在 workspace 内执行，超时 300 秒，输出最多保留 64KB。</p>
          )}
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
  const [windowId, setWindowId] = useState(0);
  const [planText, setPlanText] = useState("{}");
  const [chromeUrl, setChromeUrl] = useState("http://127.0.0.1:3000");

  useEffect(() => {
    if (plan.data) setPlanText(JSON.stringify(plan.data.plan, null, 2));
  }, [plan.data]);
  useEffect(() => {
    if (!windowId && windows.data?.windows.length) setWindowId(windows.data.windows[0].windowId);
  }, [windowId, windows.data?.windows]);

  const savePlan = useMutation({
    mutationFn: () => api.saveRecordPlan(task.id, parseJsonObject(planText, "录屏计划")),
    onSuccess: (result) => {
      toast(result.checked.ok ? "录屏计划通过" : "录屏计划未通过", result.checked.ok ? "ok" : "error");
      void queryClient.invalidateQueries({ queryKey: ["record-plan", task.id] });
    },
    onError: (error) => toast(error instanceof Error ? error.message : String(error), "error"),
  });
  const generatePlan = useMutation({
    mutationFn: async (mode: "terminal" | "web") => {
      const force = Boolean(plan.data?.exists) && window.confirm("已有录屏计划，确认用模板覆盖？");
      return api.generateRecordPlan(task.id, mode, force);
    },
    onSuccess: (result) => {
      setPlanText(JSON.stringify(result.plan, null, 2));
      toast("录屏计划模板已生成，请补全真实命令", "ok");
      void queryClient.invalidateQueries({ queryKey: ["record-plan", task.id] });
    },
    onError: (error) => toast(error instanceof Error ? error.message : String(error), "error"),
  });
  const start = useMutation({
    mutationFn: () => api.startRecording(task.id, windowId),
    onSuccess: () => {
      toast("3 秒后开始录制，请不要切换前台", "warn");
      void queryClient.invalidateQueries({ queryKey: ["recording-status"] });
    },
    onError: (error) => toast(error instanceof Error ? error.message : String(error), "error"),
  });
  const stop = useMutation({
    mutationFn: () => api.stopRecording(task.id),
    onSuccess: () => {
      toast("录屏已停止，正在读取校验结果", "ok");
      void queryClient.invalidateQueries();
    },
    onError: (error) => toast(error instanceof Error ? error.message : String(error), "error"),
  });
  const chromeStart = useMutation({
    mutationFn: () => api.chromeStart(task.id, chromeUrl),
    onSuccess: () => {
      toast("独立 Chrome 已启动", "ok");
      void windows.refetch();
    },
    onError: (error) => toast(error instanceof Error ? error.message : String(error), "error"),
  });
  const chromeStop = useMutation({
    mutationFn: () => api.chromeStop(task.id),
    onSuccess: () => toast("独立 Chrome 已关闭并清理 profile", "ok"),
    onError: (error) => toast(error instanceof Error ? error.message : String(error), "error"),
  });
  const deleteRecording = useMutation({
    mutationFn: () => api.deleteRecording(task.id),
    onSuccess: () => {
      toast("录屏文件已删除，可以重录", "warn");
      void queryClient.invalidateQueries();
    },
    onError: (error) => toast(error instanceof Error ? error.message : String(error), "error"),
  });

  const status = recording.data;
  const activeHere = status?.task_id === task.id;
  const planMode = plan.data?.plan.mode;
  const mode = typeof planMode === "string" ? planMode : "terminal";

  return (
    <div className="recording-layout">
      <Panel title="录屏计划">
        {plan.isPending ? <LoadingLines /> : null}
        <div className="form-actions plan-generator">
          <Button
            loading={generatePlan.isPending && generatePlan.variables === "terminal"}
            onClick={() => generatePlan.mutate("terminal")}
          >
            生成 Terminal 模板
          </Button>
          <Button
            loading={generatePlan.isPending && generatePlan.variables === "web"}
            onClick={() => generatePlan.mutate("web")}
          >
            生成 Web 模板
          </Button>
        </div>
        <textarea className="json-editor mono" value={planText} onChange={(event) => setPlanText(event.target.value)} />
        <div className="form-actions">
          <Button onClick={() => savePlan.mutate()} loading={savePlan.isPending}>保存并检查计划</Button>
          {plan.data ? (
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

      <Panel title="窗口与录制控制">
        {windows.isPending ? <LoadingLines rows={4} /> : null}
        {windows.isError ? <ErrorPanel error={windows.error} onRetry={() => void windows.refetch()} /> : null}
        <div className="window-list">
          {windows.data?.windows.map((window: WindowRow) => (
            <label key={window.windowId} className={windowId === window.windowId ? "is-selected" : ""}>
              <input
                type="radio"
                name="window"
                checked={windowId === window.windowId}
                onChange={() => setWindowId(window.windowId)}
              />
              <span>
                <strong>{window.app}</strong>
                <small>{window.title || "(无标题)"}</small>
              </span>
              <span className="mono">{window.width}×{window.height}</span>
              <code>#{window.windowId}</code>
            </label>
          ))}
        </div>
        <div className="record-controls">
          {status?.pending && activeHere ? (
            <div className="countdown">
              <span>{status.countdown_remaining ?? 0}</span>
              <small>请不要再切换前台</small>
            </div>
          ) : status?.recording && activeHere ? (
            <>
              <div className="record-timer">
                <StatusDot state="running" label="录制中" />
                <strong className="mono">{formatSeconds(status.elapsed_s)} / 01:30</strong>
              </div>
              <Meter value={status.elapsed_s} max={90} label="录制时长" tone="info" />
              <Button variant="danger" data-primary onClick={() => stop.mutate()} loading={stop.isPending}>
                <Square size={14} /> 停止录制
              </Button>
            </>
          ) : (
            <Button
              variant="primary"
              data-primary
              disabled={!windowId || !plan.data?.checked.ok}
              onClick={() => start.mutate()}
              loading={start.isPending}
            >
              <Play size={14} /> 开始录制
            </Button>
          )}
          {status?.recording && !activeHere ? (
            <div className="gate-warning"><Pause size={14} /> 另一个任务正在录屏，主机级锁未释放。</div>
          ) : null}
        </div>
      </Panel>

      <Panel title="录屏校验">
        {verify.isPending ? <LoadingLines /> : null}
        {verify.isError ? <ErrorPanel error={verify.error} /> : null}
        {verify.data ? (
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
              <Button variant="danger" onClick={() => deleteRecording.mutate()} loading={deleteRecording.isPending}>
                <StopCircle size={14} /> 删除不合格录屏并重录
              </Button>
            ) : null}
          </div>
        ) : <EmptyState icon={<MonitorPlay size={28} />} title="录制结束后在这里显示原始证据校验。" />}
      </Panel>
    </div>
  );
}

export function FeedbackTab({ task }: { task: TaskDetail }) {
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const feedback = useQuery({ queryKey: ["feedback", task.id], queryFn: () => api.feedback(task.id) });
  const [text, setText] = useState("");
  const [checked, setChecked] = useState(false);

  useEffect(() => {
    if (feedback.data) setText(feedback.data.text);
  }, [feedback.data]);
  const init = useMutation({
    mutationFn: () => api.initFeedback(task.id),
    onSuccess: () => void feedback.refetch(),
    onError: (error) => toast(error instanceof Error ? error.message : String(error), "error"),
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
    onError: (error) => toast(error instanceof Error ? error.message : String(error), "error"),
  });
  const ack = useMutation({
    mutationFn: () => api.ackFeedback(task.id),
    onSuccess: () => {
      toast("人工复核确认已留痕", "ok");
      setChecked(false);
      void queryClient.invalidateQueries();
    },
    onError: (error) => toast(error instanceof Error ? error.message : String(error), "error"),
  });

  const data = feedback.data;
  return (
    <div className="feedback-layout">
      <div className="feedback-editor-panel">
        <div className="editor-toolbar">
          <div>
            <span className="eyebrow">FEEDBACK / 人写，程序只检查</span>
            <strong>证据化复盘</strong>
          </div>
          {!data?.text ? <Button onClick={() => init.mutate()} loading={init.isPending}>生成空白模板</Button> : null}
        </div>
        <CodeMirror
          value={text}
          height="430px"
          extensions={[markdown()]}
          onChange={(value) => {
            setText(value);
            setChecked(false);
          }}
          basicSetup={{ lineNumbers: true, foldGutter: true }}
        />
        <div className="feedback-actions">
          <Button data-primary onClick={() => saveAndCheck.mutate()} loading={saveAndCheck.isPending}>
            <ShieldCheck size={14} /> 保存并检查
          </Button>
          <label className="check-line">
            <input
              type="checkbox"
              checked={checked}
              disabled={!data?.checked.ok || data.checked.stale}
              onChange={(event) => setChecked(event.target.checked)}
            />
            我已逐条复核事实、分数与措辞
          </label>
          <Button
            variant="primary"
            disabled={!checked || !data?.checked.ok || data.checked.stale}
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
              <Meter value={data.chars} max={360} label="非空字" />
              <Meter value={data.anchors} max={6} label="证据锚点" />
              <Meter value={Math.min(100, Math.round(data.digits_ratio * 1000))} max={100} label="数字密度" tone="info" />
              <div className="coverage-grid">
                <span className={data.coverage.process ? "covered" : ""}>过程</span>
                <span className={data.coverage.artifact ? "covered" : ""}>产物</span>
              </div>
              <div className="check-status">
                <StatusDot
                  state={data.checked.ok ? "done" : "blocked"}
                  label={
                    data.checked.stale
                      ? "文件在外部被修改，检查结果已过期"
                      : data.checked.ok
                        ? `检查通过 · ${data.checked.score} 分`
                        : "检查未通过"
                  }
                />
                {data.checked.errors.map((error) => <p key={error} className="gate-error">{error}</p>)}
                {data.checked.warnings.map((warning) => <p key={warning} className="gate-warning">{warning}</p>)}
              </div>
              <div className="ack-status">
                <StatusDot
                  state={data.review_ack.valid ? "done" : "blocked"}
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
  const [resultFile, setResultFile] = useState("");
  const [previewName, setPreviewName] = useState("");
  const preview = useQuery({
    queryKey: ["export-preview", previewName],
    queryFn: () => api.exportPreview(previewName),
    enabled: Boolean(previewName),
  });
  const create = useMutation({
    mutationFn: () => api.export({ task_ids: [task.id] }),
    onSuccess: (result) => {
      if (!result.file) {
        toast(result.skipped[0]?.reason[0] ?? "任务未通过导出检查", "error");
        return;
      }
      setResultFile(result.file);
      setPreviewName(result.file.split("/").pop() ?? "");
      toast(`已导出 ${result.rows} 行`, "ok");
      void queryClient.invalidateQueries();
    },
    onError: (error) => toast(error instanceof Error ? error.message : String(error), "error"),
  });
  const archive = useMutation({
    mutationFn: () => api.archiveTask(task.id),
    onSuccess: (result) => {
      toast(result.release_error ? `已归档，但工程归还失败：${result.release_error}` : "已归档并归还工程", result.release_error ? "warn" : "ok");
      void queryClient.invalidateQueries();
    },
    onError: (error) => toast(error instanceof Error ? error.message : String(error), "error"),
  });

  return (
    <div className="tab-stack">
      <Panel title="11 项完成度">
        <div className="gate-checklist">
          {task.steps.map((step: Step) => (
            <div key={step.number} className={step.ok ? "is-ok" : "is-pending"}>
              <span>{step.ok ? <Check size={14} /> : step.number}</span>
              <strong>{step.name}</strong>
              <small>{step.detail}</small>
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
            variant="primary"
            data-primary
            disabled={!task.complete}
            onClick={() => create.mutate()}
            loading={create.isPending}
          >
            <Upload size={14} /> 生成 19 列 Excel
          </Button>
          {resultFile ? (
            <>
              <Button onClick={() => setPreviewName(resultFile.split("/").pop() ?? "")}>刷新预览</Button>
              <a className="button button-secondary button-default" href={`/api/exports/${encodeURIComponent(resultFile.split("/").pop() ?? "")}/download`}>
                <Download size={14} /> 下载
              </a>
              <Button
                variant="ghost"
                onClick={() => {
                  void navigator.clipboard.writeText(resultFile).then(() => toast("路径已复制", "info"));
                }}
              >
                <Copy size={14} /> 复制路径
              </Button>
            </>
          ) : null}
          <Button
            disabled={task.job_status !== "exported"}
            onClick={() => archive.mutate()}
            loading={archive.isPending}
          >
            归档并归还
          </Button>
        </div>
        {resultFile ? <p className="mono path-line">{resultFile}</p> : null}
      </Panel>

      {previewName ? (
        <Panel title={`Excel 预览 · ${previewName}`}>
          {preview.isPending ? <LoadingLines rows={6} /> : null}
          {preview.isError ? <ErrorPanel error={preview.error} /> : null}
          {preview.data ? (
            <div className="table-scroll">
              <table className="data-table preview-table">
                <thead>
                  <tr>{preview.data.headers.map((header, index) => <th key={index}>{String(header ?? "—")}</th>)}</tr>
                </thead>
                <tbody>
                  {preview.data.rows.map((row, index) => (
                    <tr key={index}>{row.map((cell, cellIndex) => <td key={cellIndex}>{String(cell ?? "—")}</td>)}</tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : null}
        </Panel>
      ) : null}
    </div>
  );
}

function DiffView({ diff }: { diff: string }) {
  const lines = useMemo(() => diff.split("\n"), [diff]);
  return (
    <pre className="diff-view">
      {lines.map((line, index) => {
        const kind = line.startsWith("+") && !line.startsWith("+++") ? "add" : line.startsWith("-") && !line.startsWith("---") ? "del" : line.startsWith("@@") ? "hunk" : "";
        return <span key={index} className={`diff-line diff-${kind || "plain"}`}>{line || " "}</span>;
      })}
    </pre>
  );
}

function parseJsonObject(value: string, label: string): Record<string, unknown> {
  const parsed: unknown = JSON.parse(value);
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error(`${label}必须是 JSON 对象`);
  }
  return parsed as Record<string, unknown>;
}

function countText(value: string): number {
  return value.replace(/\s+/g, "").length;
}

function shortSha(value: string): string {
  return value ? `${value.slice(0, 12)}…` : "—";
}

function statusLetter(status: string): string {
  return status === "added" ? "A" : status === "deleted" ? "D" : status === "renamed" ? "R" : "M";
}

function formatSeconds(value: number): string {
  const seconds = Math.max(0, Math.round(value));
  return `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
}
