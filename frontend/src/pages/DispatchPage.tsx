import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  CirclePause,
  CirclePlay,
  LockKeyhole,
  MoreHorizontal,
  PauseCircle,
  Radio,
  RefreshCw,
  ShieldCheck,
  Trash2,
  X,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { Link } from "react-router-dom";

import { api } from "../api/client";
import type {
  DispatchHold,
  DispatchLimitsRequest,
  DispatchMode,
  DispatchPreviewItem,
  DispatchQueueItem,
  DispatchSnapshot,
  DispatchUpcomingItem,
} from "../api/types";
import { IntakePanel } from "../components/IntakePanel";
import { CoreTracePanel } from "../components/task/CoreTracePanel";
import { useToast } from "../components/toast-context";
import { Badge, Button, ErrorPanel, LoadingLines, Panel, StatusDot } from "../components/ui";
import { errorText, formatDuration } from "../utils/display";
import { formatDateTime } from "../utils/time";

type QueueZone = "pending" | "processing" | "processed";
type RowAction = "pin" | "up" | "down";

const MODES: Array<{ value: DispatchMode; label: string; hint: string }> = [
  { value: "containers", label: "容器优先", hint: "保持运行中的容器数等于最大容器数，任务数随之浮动" },
  { value: "tasks", label: "任务优先", hint: "保持在手任务数等于最大任务数，容器不够时排队" },
];

const NEXT_QUEUE_LIMIT = 20;
const RECENT_DONE_LIMIT = 20;

const STATE_LABEL: Record<DispatchSnapshot["effective_state"], string> = {
  running: "运行中",
  paused: "已暂停",
  draining: "排空中",
  held: "已自动挂起",
  degraded: "部分受限",
  idle: "空闲",
};

const STAGE_LABEL: Record<string, string> = {
  pending: "待建题",
  chatgpt: "ChatGPT 起草",
  authoring: "起草",
  running: "容器运行",
  delivering: "交付",
  recording: "录屏",
  exporting: "导出",
  done: "完成",
};

const FINISHED = new Set(["done", "failed", "removed", "awaiting_audit"]);
// 与后端 IN_HAND 保持一致：需人工处理和已结束的任务不占在手。
const IN_HAND = new Set(["queued", "launching", "active", "backoff", "interrupted"]);

export function DispatchPage() {
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const [draft, setDraft] = useState<DispatchLimitsRequest | null>(null);
  const [explainFor, setExplainFor] = useState<DispatchQueueItem | null>(null);
  const [traceFor, setTraceFor] = useState<DispatchQueueItem | null>(null);
  // 与顶栏共用 ["dispatch"] 缓存，同一份快照只请求一次。
  const dispatch = useQuery({
    queryKey: ["dispatch"],
    queryFn: api.dispatch,
    refetchInterval: 2500,
  });
  const upcoming = useQuery({
    queryKey: ["dispatch-upcoming", NEXT_QUEUE_LIMIT],
    queryFn: () => api.dispatchUpcoming(NEXT_QUEUE_LIMIT),
    refetchInterval: 10_000,
  });
  const sessionStats = useQuery({
    queryKey: ["dispatch-session-stats"],
    queryFn: api.sessionStats,
    refetchInterval: 10_000,
  });

  useEffect(() => {
    const snapshot = dispatch.data;
    if (!snapshot || draft) return;
    setDraft(limitDraft(snapshot));
  }, [dispatch.data, draft]);

  const refresh = () => void queryClient.invalidateQueries({ queryKey: ["dispatch"] });
  const state = useMutation({
    mutationFn: (desired: "running" | "paused" | "draining") => api.dispatchState(desired),
    onSuccess: (_, desired) => {
      toast(
        desired === "running" ? "调度已启动" : desired === "draining" ? "已进入排空后停止" : "调度已暂停",
        desired === "running" ? "ok" : "info",
      );
      void queryClient.invalidateQueries();
    },
    onError: (error) => toast(errorText(error), "error"),
  });
  const applyLimits = useMutation({
    mutationFn: (payload: DispatchLimitsRequest) => api.dispatchLimits(payload),
    onSuccess: (result) => {
      setDraft(limitDraft(result.snapshot));
      toast(
        result.sync?.synced
          ? `上限已生效，技能侧已同步为 ${result.sync.actual}`
          : `平台已生效，技能侧当前为 ${result.sync?.actual ?? "未知"}`,
        result.sync?.synced ? "ok" : "warn",
      );
      refresh();
    },
    onError: (error) => toast(errorText(error), "error"),
  });
  const action = useMutation({
    mutationFn: ({ item, action }: { item: DispatchQueueItem; action: RowAction }) =>
      action === "up" || action === "down"
        ? api.dispatchQueueAction(item.task_id, "move", action)
        : api.dispatchQueueAction(item.task_id, action),
    onSuccess: () => {
      toast("队列已更新", "ok");
      refresh();
    },
    onError: (error) => toast(errorText(error), "error"),
  });
  const holdProbe = useMutation({
    mutationFn: (hold: DispatchHold) => api.dispatchProbeHold(hold.id),
    onSuccess: (result) => {
      toast(result.message, result.ok ? "ok" : "warn");
      refresh();
    },
    onError: (error) => toast(errorText(error), "error"),
  });
  const probe = useMutation({
    mutationFn: () => api.dispatchProbe("llm_guard"),
    onSuccess: (result) => toast(result.message, result.ok ? "ok" : "warn"),
    onError: (error) => toast(errorText(error), "error"),
  });
  const recover = useMutation({
    mutationFn: () => api.dispatchRecover(120),
    onSuccess: (result) => {
      toast(`已重新排队 ${result.requeued} 题，保留 ${result.preserved} 题`, "ok");
      refresh();
    },
    onError: (error) => toast(errorText(error), "error"),
  });
  const clearFinished = useMutation({
    mutationFn: api.dispatchClearFinished,
    onSuccess: (result) => {
      toast(`已清理 ${result.removed} 题`, "ok");
      refresh();
    },
    onError: (error) => toast(errorText(error), "error"),
  });

  const snapshot = dispatch.data;
  const groups = useMemo(() => (snapshot ? groupQueue(snapshot.queue) : null), [snapshot]);
  const eta = useMemo(
    () => new Map((snapshot?.upcoming ?? []).map((item) => [item.task_id, item.estimated_start_at])),
    [snapshot?.upcoming],
  );

  if (dispatch.isPending) return <div className="page"><LoadingLines rows={10} /></div>;
  if (dispatch.isError || !snapshot || !groups) {
    return <div className="page"><ErrorPanel error={dispatch.error} onRetry={() => void dispatch.refetch()} /></div>;
  }
  const readonly = snapshot.engine === "v2";
  const busyId = action.isPending ? action.variables?.item.task_id : undefined;
  const pendingItems = arrangePendingQueue(groups.pending, NEXT_QUEUE_LIMIT);
  const previewItems = fillPreviewQueue(
    upcoming.data?.items ?? snapshot.upcoming,
    pendingItems.length,
    NEXT_QUEUE_LIMIT,
  );
  const processingItems = groups.processing.slice(0, NEXT_QUEUE_LIMIT);
  const processedItems = groups.processed.slice(0, RECENT_DONE_LIMIT);

  return (
    <div className="page dispatch-page">
      <DispatchHeader
        snapshot={snapshot}
        onState={(desired) => state.mutate(desired)}
        busy={state.isPending}
        readonly={readonly}
      />
      {readonly ? (
        <div className="system-banner" role="status">
          当前为 v2 调度引擎，调度页只读。将 <code>[dispatch] engine</code> 改为 <code>"v3"</code> 并重启后端即可切换；
          原生产线与全部 v2 数据保持可用。
        </div>
      ) : null}

      <CapacityStrip snapshot={snapshot} inHand={snapshot.queue.filter((item) => IN_HAND.has(item.status)).length} />

      {snapshot.holds.length ? (
        <section className="dispatch-holds" aria-label="当前挂起">
          {snapshot.holds.map((hold) => (
            <div className="dispatch-hold" key={hold.id}>
              <LockKeyhole size={16} />
              <div>
                <strong>{hold.reason}</strong>
                <span>
                  影响 {hold.scope.join("、")} ·{" "}
                  {hold.next_at ? <>下次自动探测 <Countdown at={hold.next_at} /></> : "正在自动探测"}
                  {hold.last_error ? ` · ${hold.last_error}` : ""}
                </span>
              </div>
              <Button
                size="small"
                variant="ghost"
                loading={holdProbe.isPending && holdProbe.variables?.id === hold.id}
                onClick={() => holdProbe.mutate(hold)}
              >
                立即探测
              </Button>
            </div>
          ))}
        </section>
      ) : null}

      <div className="dispatch-layout">
        <main className="dispatch-main">
          <section className="dispatch-board-toolbar">
            <div>
              <span className="eyebrow">QUEUE / NEXT 20</span>
              <strong>处理顺序</strong>
              <p>待处理取下一批工程，最多 20 道；工程不足时循环补位，相邻工程不重复。</p>
            </div>
            <div className="row-actions">
              {groups.processed.length ? (
                <Button size="small" variant="ghost" loading={clearFinished.isPending} disabled={readonly} onClick={() => clearFinished.mutate()}>
                  <Trash2 size={13} /> 清理已处理
                </Button>
              ) : null}
              <Button size="small" variant="ghost" onClick={() => void dispatch.refetch()} title="立即刷新">
                <RefreshCw size={13} />
              </Button>
            </div>
          </section>

          <div className="dispatch-board" aria-label="任务处理看板">
            <QueueLane
              zone="pending"
              title="待处理"
              count={groups.pending.length}
              shown={pendingItems.length + previewItems.length}
              previewCount={previewItems.length}
              caption={pendingItems.length ? "首项就是下一道进入处理的工程" : "按生产计划自动补入下一批工程"}
            >
              {pendingItems.map((item, index) => (
                <QueueRow
                  key={item.task_id}
                  item={item}
                  zone="pending"
                  rank={index + 1}
                  eta={eta.get(item.task_id)}
                  onExplain={() => setExplainFor(item)}
                  onTrace={() => setTraceFor(item)}
                  onAction={(name) => action.mutate({ item, action: name })}
                  busy={busyId === item.task_id}
                  readonly={readonly}
                />
              ))}
              {previewItems.map((item, index) => (
                <PreviewRow key={item.task_id} item={item} rank={pendingItems.length + index + 1} />
              ))}
              {!pendingItems.length && !previewItems.length ? (
                <LaneEmpty
                  title="暂无待处理工程"
                  body="下达或继续生产计划后，这里会自动排出最多 20 道候选；不足 20 道时循环补位。"
                />
              ) : null}
            </QueueLane>

            <QueueLane
              zone="processing"
              title="正在处理"
              count={groups.processing.length}
              shown={processingItems.length}
              caption="已经占用 ChatGPT 会话或容器的任务"
            >
              {processingItems.map((item, index) => (
                <QueueRow
                  key={item.task_id}
                  item={item}
                  zone="processing"
                  rank={index + 1}
                  eta={eta.get(item.task_id)}
                  onExplain={() => setExplainFor(item)}
                  onTrace={() => setTraceFor(item)}
                  onAction={(name) => action.mutate({ item, action: name })}
                  busy={busyId === item.task_id}
                  readonly={readonly}
                />
              ))}
              {!processingItems.length ? (
                <LaneEmpty title="当前没有运行中的任务" body="容器或会话释放后，待处理队列的第一道工程会进入这里。" />
              ) : null}
            </QueueLane>

            <QueueLane
              zone="processed"
              title="已处理"
              count={groups.processed.length}
              shown={processedItems.length}
              caption="最近完成或失败的任务"
            >
              {processedItems.map((item, index) => (
                <QueueRow
                  key={item.task_id}
                  item={item}
                  zone="processed"
                  rank={index + 1}
                  onExplain={() => setExplainFor(item)}
                  onTrace={() => setTraceFor(item)}
                  onAction={(name) => action.mutate({ item, action: name })}
                  busy={busyId === item.task_id}
                  readonly={readonly}
                />
              ))}
              {!processedItems.length ? (
                <LaneEmpty title="还没有处理结果" body="任务完成或失败后会进入这里，默认展示最近 20 条。" />
              ) : null}
            </QueueLane>
          </div>
        </main>

        <aside className="dispatch-rail">
          <Panel
            title="调度参数"
            actions={
              <span className={snapshot.limits.synced ? "sync-ok" : "sync-warn"} title="平台上限与技能 claude.maxContainers 是否一致">
                {snapshot.limits.synced ? "技能已同步" : `技能侧 ${snapshot.limits.skill_limit || "—"}`}
              </span>
            }
          >
            {draft ? (
              <LimitForm
                snapshot={snapshot}
                draft={draft}
                setDraft={setDraft}
                readonly={readonly}
                applying={applyLimits.isPending}
                onApply={() => applyLimits.mutate(draft)}
              />
            ) : null}
          </Panel>

          <Panel
            title="运行健康"
            actions={<Badge tone={snapshot.holds.length ? "warn" : "ok"}>{snapshot.holds.length ? `${snapshot.holds.length} 条挂起` : "正常"}</Badge>}
          >
            <div className="panel-body dispatch-probes">
              <ProbeLine label="网关" ok={!snapshot.holds.some((hold) => hold.source === "llm_guard")} onTest={() => probe.mutate()} busy={probe.isPending} />
              <ProbeLine label="Docker" ok={Boolean((snapshot.pools.containers?.limit || 0) > 0 && snapshot.effective_state !== "held")} />
              <ProbeLine
                label="守护"
                ok={snapshot.guard.mode !== "off"}
                value={`${snapshot.guard.mode === "enforce" ? "自动处置" : snapshot.guard.mode === "observe" ? "仅观察" : "关闭"} · 命中 ${snapshot.guard.count}`}
              />
              {snapshot.guard.hits.length ? (
                <ul className="dispatch-guard-list">
                  {snapshot.guard.hits.map((hit) => (
                    <li key={String(hit.id)}>
                      <strong>{String(hit.kind)}</strong>
                      <span>{String(hit.reason)}</span>
                    </li>
                  ))}
                </ul>
              ) : null}
              <Button size="small" variant="ghost" loading={recover.isPending} disabled={readonly} onClick={() => recover.mutate()}>
                <ShieldCheck size={13} /> 清理挂起期间残留
              </Button>
            </div>
          </Panel>

          <Panel title="近 24 小时">
            <div className="panel-body dispatch-metrics">
              <Metric label="完成" value={snapshot.throughput.completed} />
              <Metric label="失败" value={snapshot.throughput.failed + snapshot.throughput.quarantined} />
              <Metric label="容器利用率" value={`${Math.round(snapshot.throughput.container_utilization * 100)}%`} />
              <Metric label="单题运行中位" value={formatMinutes(snapshot.throughput.medians_seconds.running_s ?? 0)} />
            </div>
          </Panel>

          <Panel title="会话质量">
            <div className="panel-body dispatch-metrics">
              <Metric
                label="继续率"
                value={`${Math.round((sessionStats.data?.continueRate ?? 0) * 100)}%`}
              />
              <Metric
                label="每容器小时"
                value={(sessionStats.data?.sessionsPerContainerHour ?? 0).toFixed(2)}
              />
              <Metric
                label="接受 / 含继续"
                value={`${sessionStats.data?.accepted ?? 0} / ${sessionStats.data?.acceptedWithContinue ?? 0}`}
              />
              <Metric
                label="主要终止原因"
                value={sessionStats.data?.abortReasons[0]?.kind ?? "—"}
              />
            </div>
          </Panel>

          <IntakePanel />
        </aside>
      </div>

      {explainFor ? <ExplainDrawer item={explainFor} onClose={() => setExplainFor(null)} /> : null}
      {traceFor ? <LiveTraceDrawer item={traceFor} onClose={() => setTraceFor(null)} /> : null}
    </div>
  );
}

function DispatchHeader({
  snapshot,
  onState,
  busy,
  readonly,
}: {
  snapshot: DispatchSnapshot;
  onState: (desired: "running" | "paused" | "draining") => void;
  busy: boolean;
  readonly: boolean;
}) {
  const running = snapshot.desired_state === "running";
  return (
    <header className="dispatch-head">
      <div className="dispatch-head-title">
        <div className="dispatch-title-line">
          <div>
            <span className="eyebrow">DISPATCH / LIVE QUEUE</span>
            <h1>任务队列</h1>
          </div>
          <StatusDot
            state={snapshot.effective_state === "running" ? "running" : snapshot.effective_state === "held" ? "blocked" : "idle"}
            label={STATE_LABEL[snapshot.effective_state]}
            pulse={snapshot.effective_state === "running"}
          />
        </div>
        <p className="dispatch-sentence" data-state={snapshot.effective_state}>{snapshot.sentence}</p>
      </div>
      <div className="dispatch-head-actions">
        {running ? (
          <Button loading={busy} disabled={readonly} onClick={() => onState("paused")}><CirclePause size={14} /> 暂停</Button>
        ) : (
          <Button variant="primary" loading={busy} disabled={readonly} onClick={() => onState("running")}>
            <CirclePlay size={14} /> {snapshot.desired_state === "paused" ? "继续" : "启动"}
          </Button>
        )}
        {snapshot.desired_state !== "draining" ? (
          <Button size="small" variant="ghost" disabled={readonly || busy} onClick={() => onState("draining")} title="不再启动新任务，在手任务跑完后停止">
            <PauseCircle size={13} /> 排空后停止
          </Button>
        ) : null}
      </div>
    </header>
  );
}

function CapacityStrip({ snapshot, inHand }: { snapshot: DispatchSnapshot; inHand: number }) {
  const now = useNow();
  const pool = (name: string) => snapshot.pools[name] ?? { used: 0, limit: 0 };
  const creation = snapshot.creation;
  const containerLimit = creation?.containers.limit ?? snapshot.limits.containers;
  const containerUsed = creation?.containers.used ?? pool("containers").used;
  const taskLimit = creation?.max_tasks ?? snapshot.limits.max_tasks;
  const tasksFirst = (snapshot.limits.mode ?? "containers") === "tasks";
  const waitingContinue = snapshot.session_capacity?.waiting ?? 0;
  const cooldownLeft = creation?.next_allowed_at
    ? Math.max(0, Math.round((new Date(creation.next_allowed_at).getTime() - now) / 1000))
    : 0;
  const cooldown = creation?.cooldown_seconds ?? snapshot.limits.create_cooldown_seconds;
  const containerNote = creation
    ? `运行 ${creation.containers.live} · 待起 ${creation.containers.waiting + creation.containers.extra}`
    : undefined;
  const cells: Array<{ label: string; used: number; limit: number; note?: string; major?: boolean; tone?: string; unbounded?: boolean }> = [
    { label: "容器", used: containerUsed, limit: containerLimit, major: true, note: containerNote },
    tasksFirst
      ? { label: "任务", used: inHand, limit: taskLimit, major: true }
      : { label: "任务", used: inHand, limit: 0, major: true, unbounded: true, tone: "free", note: "容器优先，不限任务数" },
    {
      label: "建题冷却",
      used: cooldownLeft,
      limit: cooldown,
      major: true,
      tone: cooldownLeft > 0 ? "full" : "free",
      note: cooldownLeft > 0 ? `${cooldownLeft} 秒后可建下一题` : "冷却已结束",
    },
    {
      label: "等待继续",
      used: waitingContinue,
      limit: containerLimit,
      note: waitingContinue ? "容器仍占名额，到达时间后自动发送“继续”" : "当前没有等待项",
    },
    { label: "录屏", used: pool("recording").used, limit: pool("recording").limit },
  ];
  return (
    <section className="dispatch-capacity" aria-label="资源占用" data-mode={tasksFirst ? "tasks" : "containers"}>
      {cells.map((cell) => {
        const tone =
          cell.tone ?? (cell.used > cell.limit ? "over" : cell.limit > 0 && cell.used >= cell.limit ? "full" : "free");
        return (
          <div key={cell.label} className="dispatch-capacity-cell" data-tone={tone} data-major={cell.major || undefined}>
            <span>{cell.label}</span>
            <strong className="mono">
              {cell.used}{cell.unbounded ? null : <small>/{cell.limit}</small>}
            </strong>
            <div className="dispatch-capacity-bar" aria-hidden="true">
              <i style={{ width: `${cell.limit ? Math.min(100, (cell.used / cell.limit) * 100) : 0}%` }} />
            </div>
            <em>
              {cell.tone
                ? cell.note
                : tone === "over"
                  ? "超出上限，收敛中"
                  : tone === "full"
                    ? "已满"
                    : cell.note ?? `空闲 ${Math.max(0, cell.limit - cell.used)}`}
            </em>
          </div>
        );
      })}
      {creation && !creation.allowed && snapshot.desired_state === "running" ? (
        <p className="dispatch-capacity-notice" role="status">
          暂不建新题：{creation.text}
        </p>
      ) : null}
      {waitingContinue ? (
        <p className="dispatch-capacity-notice" role="status">
          {snapshot.session_capacity?.label}；等待期间容器继续占用名额，不按卡死处理。
        </p>
      ) : null}
    </section>
  );
}

function QueueLane({
  zone,
  title,
  count,
  shown,
  previewCount = 0,
  caption,
  children,
}: {
  zone: QueueZone;
  title: string;
  count: number;
  shown: number;
  previewCount?: number;
  caption: string;
  children: ReactNode;
}) {
  const hidden = Math.max(0, count - shown);
  return (
    <section className="dispatch-lane" data-zone={zone}>
      <header className="dispatch-lane-head">
        <div>
          <span>{title === "待处理" ? "NEXT" : title === "正在处理" ? "LIVE" : "RECENT"}</span>
          <h2>
            {title}<b>{count}</b>
            {previewCount ? <em>+{previewCount} 候补</em> : null}
          </h2>
          <p>{caption}</p>
        </div>
      </header>
      <div className="dispatch-lane-body">{children}</div>
      {hidden ? <footer className="dispatch-lane-foot">另有 {hidden} 条未展开</footer> : null}
    </section>
  );
}

function LaneEmpty({ title, body }: { title: string; body: string }) {
  return (
    <div className="dispatch-lane-empty">
      <strong>{title}</strong>
      <p>{body}</p>
    </div>
  );
}

function PreviewRow({ item, rank }: { item: DispatchPreviewItem; rank: number }) {
  return (
    <article className="dispatch-queue-row dispatch-preview-row" data-zone="pending" data-preview>
      <span className="dispatch-queue-index mono">{String(rank).padStart(2, "0")}</span>
      <div className="dispatch-queue-copy">
        <strong title={item.task_name}>{item.task_name}</strong>
        <span>候补工程 · {item.engineering_id}</span>
        <small>{item.cycle ? `第 ${item.cycle + 1} 轮轮替` : "本轮首次进入"}</small>
      </div>
      <div className="dispatch-queue-meta mono">
        <span>已用 {item.used}</span>
        <em>{item.last_used_at ? "有历史" : "未使用"}</em>
      </div>
    </article>
  );
}

function QueueRow({
  item,
  zone,
  rank,
  eta,
  onExplain,
  onTrace,
  onAction,
  busy,
  readonly,
}: {
  item: DispatchQueueItem;
  zone: QueueZone;
  rank: number;
  eta?: string;
  onExplain: () => void;
  onTrace: () => void;
  onAction: (name: RowAction) => void;
  busy: boolean;
  readonly: boolean;
}) {
  const needs = item.status === "quarantined";
  const pending = zone === "pending";
  const until = item.wait_until || item.not_before;
  return (
    <article
      className="dispatch-queue-row"
      data-status={item.status}
      data-zone={zone}
      data-next={pending && rank === 1 && !needs || undefined}
    >
      <span className="dispatch-queue-index mono">{String(rank).padStart(2, "0")}</span>
      <div className="dispatch-queue-copy">
        <div className="dispatch-queue-status">
          <StatusDot
            state={item.active ? "running" : needs ? "blocked" : item.status === "done" ? "exported" : zone === "processed" ? "idle" : "queued"}
            label={needs ? "已失败" : STAGE_LABEL[item.stage] ?? item.stage}
            pulse={item.active}
          />
          {pending && !needs ? <span>下一道工程</span> : null}
          {needs ? (
            <span data-alert>
              运行失败，稍后自动清理并补位
            </span>
          ) : null}
        </div>
        <Link className="dispatch-queue-name" to={`/tasks/${item.task_id}`} title={item.task_name}>
          {item.task_name}
        </Link>
        {zone === "processing" ? (
          <span className="dispatch-queue-note">
            已运行 <Elapsed since={item.updated_at} />
            {item.session_state === "waiting_continue"
              ? ` · 等待继续 · 已用 ${item.continue_used ?? 0}/2`
              : item.session_state === "holding"
                ? " · 等待人工处理"
                : item.session_state === "manual_input"
                  ? " · 需要人工输入"
                  : item.awaiting_platform
                    ? " · 已提交，等待 SOLO2 质检反馈（不占会话名额）"
                    : ` · ${item.engineering_id || "工程信息同步中"}`}
          </span>
        ) : zone === "processed" ? (
          <span className="dispatch-queue-note" title={item.status === "failed" ? item.wait_text : undefined}>
            {item.status === "done" ? "已完成" : item.status === "failed" ? "执行失败，已自动补新题" : "已结束"}
            {" · "}{formatDateTime(item.updated_at)}
          </span>
        ) : (
          <button type="button" className="dispatch-queue-reason" onClick={onExplain} title="查看门禁明细">
            {item.reason}
          </button>
        )}
      </div>
      <div className="dispatch-queue-meta mono">
        {pending && until ? <span>排队重跑 <Countdown at={until} /></span> : null}
        {pending && !until && eta ? <span>预计 <Countdown at={eta} /></span> : null}
        {item.desktop_recovery_attempts ? (
          <span title={item.desktop_recovery_error || item.autopilot_detail || "正在补按发送，不会重新创建任务"}>
            <Badge tone="warn">自动补发 {item.desktop_recovery_attempts}/2</Badge>
          </span>
        ) : null}
        {item.total_attempts > 1 ? <span>尝试 {item.total_attempts}</span> : null}
      </div>
      <div className="dispatch-queue-actions">
        {zone === "processing" ? (
          <Button className="dispatch-live-link" size="small" variant="ghost" onClick={onTrace} title="核心代码执行轨迹">
            <Radio size={12} /> 执行轨迹
          </Button>
        ) : null}
        {zone !== "processed" ? (
          <details className="dispatch-more">
            <summary title="更多操作" aria-label="更多操作"><MoreHorizontal size={14} /></summary>
            <div>
              {pending ? (
                <>
                  <button type="button" disabled={readonly || busy} onClick={() => onAction("pin")} title="置顶">
                    置顶<small>放到下一批队列最前面</small>
                  </button>
                  <button type="button" disabled={readonly || busy} onClick={() => onAction("up")} title="上移">
                    上移<small>提前一位处理</small>
                  </button>
                  <button type="button" disabled={readonly || busy} onClick={() => onAction("down")} title="下移">
                    下移<small>延后一位处理</small>
                  </button>
                </>
              ) : null}
              {!item.active ? (
                <button type="button" onClick={onExplain}>
                  门禁明细<small>逐条查看当前等待原因</small>
                </button>
              ) : null}
            </div>
          </details>
        ) : null}
      </div>
    </article>
  );
}

function LimitForm({
  snapshot,
  draft,
  setDraft,
  readonly,
  applying,
  onApply,
}: {
  snapshot: DispatchSnapshot;
  draft: DispatchLimitsRequest;
  setDraft: (draft: DispatchLimitsRequest) => void;
  readonly: boolean;
  applying: boolean;
  onApply: () => void;
}) {
  const baseline = limitDraft(snapshot);
  const dirty = JSON.stringify(draft) !== JSON.stringify(baseline);
  const containers = draft.containers ?? snapshot.limits.containers;
  const maxTasks = draft.max_tasks ?? snapshot.limits.max_tasks;
  const cooldown = draft.create_cooldown_seconds ?? snapshot.limits.create_cooldown_seconds;
  const mode = draft.mode ?? snapshot.limits.mode ?? "containers";
  const tasksFirst = mode === "tasks";
  return (
    <fieldset className="dispatch-limit-form panel-body" disabled={readonly}>
      <div className="dispatch-limit-row dispatch-mode-row">
        <span>调度模式</span>
        <div className="dispatch-mode" role="radiogroup" aria-label="调度模式">
          {MODES.map((option) => (
            <button
              key={option.value}
              type="button"
              role="radio"
              aria-checked={mode === option.value}
              data-active={mode === option.value || undefined}
              title={option.hint}
              onClick={() => setDraft({ ...draft, mode: option.value })}
            >
              {option.label}
            </button>
          ))}
        </div>
      </div>
      <LimitNumber
        label="最大容器数"
        hint={tasksFirst ? "任务优先时仍是容器的物理上限，多出来的题排队等容器" : "建题只看这个数：容器占用加 1 不超过它"}
        value={containers}
        min={1}
        max={8}
        onChange={(value) => setDraft({ ...draft, containers: value })}
      />
      <LimitNumber
        label="最大任务数"
        hint={tasksFirst ? "建题只看这个数：已建题、未结束的任务总数上限" : "容器优先模式不使用此值，切到任务优先后生效"}
        value={maxTasks}
        min={1}
        max={50}
        inactive={!tasksFirst}
        onChange={(value) => setDraft({ ...draft, max_tasks: value })}
      />
      <LimitNumber
        label="建题冷却"
        hint="两次建题之间至少间隔的秒数，重启后接着计时"
        suffix="秒"
        value={cooldown}
        min={20}
        max={3600}
        onChange={(value) => setDraft({ ...draft, create_cooldown_seconds: value })}
      />
      <p className="dispatch-limit-note">
        {tasksFirst
          ? "任务优先：在手任务数小于最大任务数、距上次建题已满冷却，就建下一题；容器占满时题目排队等容器。"
          : "容器优先：容器占用（运行中 + 已建题待起容器）加 1 不超过最大容器数、距上次建题已满冷却，就建下一题；不限任务数。"}
        {tasksFirst && maxTasks < containers ? " 最大任务数小于最大容器数时，容器跑不满。" : ""}
        {" 每次只建一题。"}
      </p>
      <div className="dispatch-limit-actions" data-dirty={dirty || undefined}>
        <span>{dirty ? "有未应用的修改" : ""}</span>
        <Button size="small" variant="ghost" disabled={!dirty} onClick={() => setDraft(baseline)}>还原</Button>
        <Button variant="primary" size="small" disabled={!dirty} loading={applying} onClick={onApply}>应用</Button>
      </div>
    </fieldset>
  );
}

function ExplainDrawer({ item, onClose }: { item: DispatchQueueItem; onClose: () => void }) {
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="dispatch-drawer-layer" onMouseDown={onClose}>
      <aside className="dispatch-drawer" role="dialog" aria-label="门禁明细" onMouseDown={(event) => event.stopPropagation()}>
        <header>
          <div>
            <span className="eyebrow">WHY NOT RUNNING</span>
            <h2>{item.task_name}</h2>
          </div>
          <Button size="small" variant="ghost" onClick={onClose} title="关闭（Esc）"><X size={14} /></Button>
        </header>
        <p className="dispatch-reason-large">{item.reason}</p>
        <ol className="dispatch-gate-list">
          {item.gate.checks?.map((check) => (
            <li key={check.gate} data-ok={check.ok}>
              <span>{check.ok ? "✓" : "✗"}</span>
              <strong>{check.gate}</strong>
              <div>
                <p>{check.text || (check.ok ? "通过" : check.code)}</p>
                {check.code ? <code>{check.code}</code> : null}
              </div>
            </li>
          ))}
        </ol>
      </aside>
    </div>
  );
}

function LiveTraceDrawer({ item, onClose }: { item: DispatchQueueItem; onClose: () => void }) {
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="dispatch-drawer-layer dispatch-trace-layer" onMouseDown={onClose}>
      <aside
        className="dispatch-drawer dispatch-trace-drawer"
        role="dialog"
        aria-label={`核心代码执行轨迹：${item.task_name}`}
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header>
          <div>
            <span className="eyebrow">CORE CODE TRACE</span>
            <h2>{item.task_name}</h2>
          </div>
          <div className="dispatch-trace-head-actions">
            <Link to={`/tasks/${item.task_id}?tab=timeline`}>完整任务页</Link>
            <Button size="small" variant="ghost" onClick={onClose} title="关闭（Esc）"><X size={14} /></Button>
          </div>
        </header>
        <CoreTracePanel taskId={item.task_id} />
      </aside>
    </div>
  );
}

function LimitNumber({
  label,
  value,
  min,
  max,
  suffix,
  hint,
  inactive,
  onChange,
}: {
  label: string;
  value: number;
  min: number;
  max: number;
  suffix?: string;
  hint?: string;
  inactive?: boolean;
  onChange: (value: number) => void;
}) {
  return (
    <label className="dispatch-limit-row" title={hint} data-inactive={inactive || undefined}>
      <span>{label}</span>
      <input className="compact-number" type="number" aria-label={label} min={min} max={max} value={value} onChange={(event) => onChange(Number(event.target.value))} />
      <small>{inactive ? "未启用" : suffix ?? ""}</small>
    </label>
  );
}

function ProbeLine({ label, ok, value, onTest, busy }: { label: string; ok: boolean; value?: string; onTest?: () => void; busy?: boolean }) {
  return (
    <div className="dispatch-probe-line">
      <StatusDot state={ok ? "done" : "blocked"} label={label} />
      <span>{value ?? (ok ? "正常" : "异常")}</span>
      {onTest ? <Button size="small" variant="ghost" loading={busy} onClick={onTest}>测试</Button> : <span />}
    </div>
  );
}

function Metric({ label, value }: { label: string; value: ReactNode }) {
  return <div><span>{label}</span><strong>{value}</strong></div>;
}

/** 每秒只重绘自身，避免整页跟着时钟刷新。 */
function useNow(): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);
  return now;
}

function Countdown({ at }: { at: string }) {
  const now = useNow();
  return <time dateTime={at}>{formatCountdown(at, now)}</time>;
}

function Elapsed({ since }: { since: string }) {
  const now = useNow();
  const start = new Date(since).getTime();
  return <>{Number.isNaN(start) ? "—" : formatDuration((now - start) / 1000)}</>;
}

function groupQueue(queue: DispatchQueueItem[]): Record<QueueZone, DispatchQueueItem[]> {
  const groups: Record<QueueZone, DispatchQueueItem[]> = { pending: [], processing: [], processed: [] };
  for (const item of queue) {
    if (item.active) groups.processing.push(item);
    else if (FINISHED.has(item.status)) groups.processed.push(item);
    else groups.pending.push(item);
  }
  groups.pending.sort((a, b) => {
    const urgent = Number(b.status === "quarantined") - Number(a.status === "quarantined");
    return urgent || (a.queue_position ?? 1e9) - (b.queue_position ?? 1e9);
  });
  groups.processing.sort((a, b) => (a.queue_position ?? 1e9) - (b.queue_position ?? 1e9));
  groups.processed.sort((a, b) => new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime());
  return groups;
}

function arrangePendingQueue(items: DispatchQueueItem[], limit: number): DispatchQueueItem[] {
  const groups = new Map<string, DispatchQueueItem[]>();
  for (const item of items) {
    const key = item.engineering_id || item.project_id || item.task_id;
    const rows = groups.get(key) ?? [];
    rows.push(item);
    groups.set(key, rows);
  }
  const arranged: DispatchQueueItem[] = [];
  let previous = "";
  while (arranged.length < items.length && arranged.length < limit) {
    let picked = false;
    for (const [key, rows] of groups) {
      if (!rows.length || key === previous) continue;
      const item = rows.shift();
      if (!item) continue;
      arranged.push(item);
      previous = key;
      picked = true;
      break;
    }
    if (!picked) {
      const fallback = [...groups.entries()].find(([, rows]) => rows.length);
      const item = fallback?.[1].shift();
      if (!item) break;
      arranged.push(item);
      previous = fallback?.[0] ?? "";
    }
  }
  return arranged;
}

function fillPreviewQueue(
  items: Array<DispatchUpcomingItem | DispatchPreviewItem> | undefined,
  current: number,
  limit: number,
): DispatchPreviewItem[] {
  if (!items?.length || current >= limit) return [];
  const previews = items.filter((item): item is DispatchPreviewItem => "preview" in item && item.preview === true);
  const arranged: DispatchPreviewItem[] = [];
  let previous = "";
  for (const item of previews) {
    if (arranged.length >= limit - current) break;
    if (item.engineering_id === previous) continue;
    arranged.push(item);
    previous = item.engineering_id;
  }
  return arranged;
}

function limitDraft(snapshot: DispatchSnapshot): DispatchLimitsRequest {
  return {
    mode: snapshot.limits.mode ?? "containers",
    containers: snapshot.limits.containers,
    max_tasks: snapshot.limits.max_tasks,
    create_cooldown_seconds: snapshot.limits.create_cooldown_seconds,
  };
}

function formatCountdown(value: string, now: number): string {
  if (!value) return "—";
  const delta = Math.round((new Date(value).getTime() - now) / 1000);
  if (Number.isNaN(delta)) return "—";
  if (delta <= 0) return "即将";
  if (delta < 60) return `${delta} 秒后`;
  if (delta < 3600) return `${Math.ceil(delta / 60)} 分钟后`;
  return formatDateTime(value);
}

function formatMinutes(seconds: number): string {
  if (!seconds) return "—";
  return `${Math.max(1, Math.round(seconds / 60))} 分钟`;
}
