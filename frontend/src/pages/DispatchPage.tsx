import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  CirclePause,
  CirclePlay,
  ChevronDown,
  LockKeyhole,
  MoreHorizontal,
  PauseCircle,
  Radio,
  RefreshCw,
  ShieldCheck,
  SquareTerminal,
  X,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import { Link } from "react-router-dom";

import { api } from "../api/client";
import type {
  AccountRole,
  DispatchHold,
  DispatchLimitsRequest,
  DispatchMode,
  DispatchPreviewItem,
  DispatchQueueItem,
  DispatchQueueSnapshot,
  AccountQuota,
} from "../api/types";
import { IntakePanel } from "../components/IntakePanel";
import { CoreTracePanel } from "../components/task/CoreTracePanel";
import { useToast } from "../components/toast-context";
import { Badge, Button, ErrorPanel, LoadingLines, Panel, StatusDot } from "../components/ui";
import { DISPATCH_STATE_LABEL, errorText } from "../utils/display";
import { formatDateTime } from "../utils/time";

type QueueZone = "pending" | "processing";
type RowAction = "pin" | "up" | "down";

const MODES: Array<{ value: DispatchMode; label: string; hint: string }> = [
  { value: "containers", label: "容器优先", hint: "保持运行中的容器数等于最大容器数，任务数随之浮动" },
  { value: "tasks", label: "任务优先", hint: "保持在手任务数等于最大任务数，容器不够时排队" },
];

const NEXT_QUEUE_LIMIT = 20;

const STAGE_LABEL: Record<string, string> = {
  pending: "待建题",
  chatgpt: "ChatGPT 起草",
  authoring: "起草",
  running: "容器运行",
  delivering: "交付",
  recording: "录屏",
  exporting: "导出",
  reviewing: "提交与质检",
  waiting_quota: "待次日提交",
  done: "完成",
};

const HIDDEN_STATUSES = new Set(["done", "failed", "removed", "stopped", "quarantined", "awaiting_audit"]);
// 与后端 IN_HAND 保持一致：需人工处理和已结束的任务不占在手。
const IN_HAND = new Set(["queued", "launching", "active", "backoff", "interrupted"]);

export function DispatchPage() {
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const [edits, setEdits] = useState<DispatchLimitsRequest>({});
  const [limitsOpen, setLimitsOpen] = useState(false);
  const [monitorOpen, setMonitorOpen] = useState(false);
  const [detail, setDetail] = useState<{ id: string; view: "wait" | "trace" } | null>(null);
  const [pendingLimit, setPendingLimit] = useState(NEXT_QUEUE_LIMIT);
  const [processingLimit, setProcessingLimit] = useState(NEXT_QUEUE_LIMIT);
  const dispatch = useQuery({
    queryKey: ["dispatch"],
    queryFn: api.dispatch,
    refetchInterval: (query) => query.state.data?.desired_state === "paused" ? 10_000 : 5000,
  });
  const sessionStats = useQuery({
    queryKey: ["dispatch-session-stats"],
    queryFn: api.sessionStats,
    enabled: monitorOpen,
    refetchInterval: monitorOpen ? 30_000 : false,
  });

  const throughput = useQuery({
    queryKey: ["dispatch-throughput", 24], queryFn: () => api.dispatchThroughput(24),
    enabled: monitorOpen, refetchInterval: monitorOpen ? 30_000 : false,
  });
  const system = useQuery({ queryKey: ["system-status"], queryFn: api.systemStatus, enabled: monitorOpen, staleTime: 10_000 });

  const refresh = () => void queryClient.invalidateQueries({ queryKey: ["dispatch"] });
  const state = useMutation({
    mutationFn: (desired: "running" | "paused" | "draining") => api.dispatchState(desired),
    onSuccess: (_, desired) => {
      toast(
        desired === "running" ? "调度已启动" : desired === "draining" ? "已进入排空后停止" : "调度已暂停",
        desired === "running" ? "ok" : "info",
      );
      refresh();
      void queryClient.invalidateQueries({ queryKey: ["system-status"] });
      void queryClient.invalidateQueries({ queryKey: ["intake"] });
    },
    onError: (error) => toast(errorText(error), "error"),
  });
  const applyLimits = useMutation({
    mutationFn: (payload: DispatchLimitsRequest) => api.dispatchLimits(payload),
    onSuccess: (result) => {
      setEdits({});
      queryClient.setQueryData<DispatchQueueSnapshot>(["dispatch"], (old) => old && ({
        ...old, limits: result.snapshot.limits, elastic: result.snapshot.elastic,
      }));
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
  const openTerminals = useMutation({
    mutationFn: api.openAllWorkTerminals,
    onSuccess: (result) => {
      toast(
        result.count
          ? `已显示 ${result.count} 个正在工作的终端${result.reused ? `，复用 ${result.reused} 个` : ""}`
          : "当前没有正在工作的终端",
        result.ok ? "ok" : "warn",
      );
    },
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
  const snapshot = dispatch.data;
  const groups = useMemo(() => (snapshot ? groupQueue(snapshot.queue) : null), [snapshot]);
  const intake = useQuery({ queryKey: ["intake"], queryFn: api.intake, staleTime: 5000 });
  const plan = intake.data?.plan;
  const showCandidates = !!groups && groups.pending.length < NEXT_QUEUE_LIMIT &&
    (plan?.status === "running" || plan?.status === "paused");
  const upcoming = useQuery({
    queryKey: ["dispatch-upcoming", plan?.id, plan?.project_id, plan?.source_changed_at, plan?.filters, plan?.exclude_delivered, plan?.max_uses,
      snapshot?.submission_quota?.date, snapshot?.submission_quota?.selected],
    enabled: showCandidates,
    queryFn: async ({ signal }) => {
      const batch = await api.dispatchUpcoming(NEXT_QUEUE_LIMIT, signal);
      if (batch.items.length >= NEXT_QUEUE_LIMIT || batch.items.some(item => "preview" in item)) return batch;
      // 首次读取会在服务端异步准备题源，稍后再取一次已预热的清单。
      await new Promise(resolve => setTimeout(resolve, 1500));
      return api.dispatchUpcoming(NEXT_QUEUE_LIMIT, signal);
    },
    staleTime: 15_000,
    refetchInterval: 25_000,
  });
  const eta = useMemo(
    () => new Map((snapshot?.upcoming ?? []).map((item) => [item.task_id, item.estimated_start_at])),
    [snapshot?.upcoming],
  );

  if (dispatch.isPending) return <div className="page"><LoadingLines rows={10} /></div>;
  if (!snapshot || !groups) {
    return <div className="page"><ErrorPanel error={dispatch.error} onRetry={() => void dispatch.refetch()} /></div>;
  }
  const readonly = snapshot.engine === "v2" || dispatch.isError;
  const selected = detail && snapshot.queue.find((item) => item.task_id === detail.id);
  const baseline = limitDraft(snapshot);
  const draft = { ...baseline, ...edits, elastic: { ...baseline.elastic, ...edits.elastic } };
  const setDraft = (next: DispatchLimitsRequest) => setEdits(limitChanges(baseline, next));
  const busyId = action.isPending ? action.variables?.item.task_id : undefined;
  const pendingItems = groups.pending.slice(0, pendingLimit);
  const processingItems = groups.processing.slice(0, processingLimit);
  const candidateLane = snapshot.submission_quota?.selected;
  const candidateProject = candidateLane ? snapshot.submission_quota?.[candidateLane]?.project_id : plan?.project_id;
  const candidateLimit = Math.min(NEXT_QUEUE_LIMIT - groups.pending.length,
    candidateLane ? snapshot.submission_quota?.[candidateLane]?.remaining ?? 0 : NEXT_QUEUE_LIMIT);
  const occupied = new Set([...groups.pending, ...groups.processing].map(item => `${item.account_role || ""}/${item.engineering_id}`));
  const candidates: DispatchPreviewItem[] = [];
  const candidateSlots = new Map<string, number>();
  const candidateCounts = new Map<string, number>();
  if (showCandidates) {
    for (const item of upcoming.data?.items ?? []) {
      if (!("preview" in item) || item.project_id !== candidateProject || (item.account_role && item.account_role !== candidateLane)
          || occupied.has(`${item.account_role || ""}/${item.engineering_id}`)) continue;
      if (candidates.length >= candidateLimit) break;
      candidates.push(item);
      occupied.add(`${item.account_role || ""}/${item.engineering_id}`);
      const role = item.account_role || candidateLane || "";
      const quota = role ? snapshot.submission_quota?.[role] : undefined;
      const taken = quota?.submitted != null ? quota.submitted + (quota.reserved ?? 0) : 0;
      const offset = candidateCounts.get(role) ?? 0;
      candidateSlots.set(item.task_id, taken + offset + 1);
      candidateCounts.set(role, offset + 1);
    }
  }

  return (
    <div className="page dispatch-page">
      <DispatchHeader
        snapshot={snapshot}
        onState={(desired) => state.mutate(desired)}
        busy={state.isPending}
        readonly={readonly}
        onRefresh={() => { void dispatch.refetch(); if (showCandidates) void upcoming.refetch(); }}
        refreshing={dispatch.isFetching}
        openingTerminals={openTerminals.isPending}
        onOpenTerminals={() => openTerminals.mutate()}
      />
      {dispatch.isError && (
        <div className="system-banner" role="status">队列刷新失败，当前显示上次获取的数据。
          <Button size="small" onClick={() => void dispatch.refetch()}>重新连接</Button>
        </div>
      )}
      {snapshot.engine === "v2" ? (
        <div className="system-banner" role="status">
          当前为 v2 调度引擎，调度页只读。将 <code>[dispatch] engine</code> 改为 <code>"v3"</code> 并重启后端即可切换；
          原生产线与全部 v2 数据保持可用。
        </div>
      ) : null}

      <CapacityStrip snapshot={snapshot} inHand={snapshot.creation?.in_hand ?? snapshot.queue.filter((item) => IN_HAND.has(item.status)).length} />

      {snapshot.submission_quota && (
        <Panel title="SOLO2 今日有效提交" actions={<div className="row-actions">
          <a className="text-link" href="https://solo2.jzxhnh.com/app" target="_blank" rel="noreferrer">打开 SOLO2</a>
          <Link className="text-link" to="/source#source-project-pools">项目池设置</Link>
        </div>}>
          <p className="form-note">{snapshot.submission_quota.date} · 每号上限 {snapshot.submission_quota.limit} 条 · 后台每分钟同步 · 北京时间 00:00 换日。仅统计当天首次提交，并扣除已废弃条目。</p>
          <div className="dispatch-quota-accounts">
            <QuotaAccount role="primary" account={snapshot.submission_quota.primary} />
            <QuotaAccount role="standby" account={snapshot.submission_quota.standby} />
          </div>
          {snapshot.submission_quota.reason && <p className="form-note" role="status">{snapshot.submission_quota.reason}</p>}
        </Panel>
      )}
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
                disabled={readonly}
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
          <div className="dispatch-board" aria-label="任务处理看板">
            <QueueLane
              zone="pending"
              title="待处理"
              count={groups.pending.length}
              shown={pendingItems.length}
              candidateCount={candidates.length}
              caption={candidates.length && !pendingItems.length ? pendingQueueHint(snapshot) : "已排队任务在前，候补工程按预计领取顺序排列。"}
            >
              {pendingItems.map((item, index) => (
                <QueueRow
                  key={item.task_id}
                  item={item}
                  zone="pending"
                  rank={index + 1}
                  submissionLimit={snapshot.submission_quota?.limit}
                  last={index + 1 === groups.pending.length}
                  eta={eta.get(item.task_id)}
                  onExplain={() => setDetail({ id: item.task_id, view: "wait" })}
                  onTrace={() => setDetail({ id: item.task_id, view: "trace" })}
                  onAction={(name) => action.mutate({ item, action: name })}
                  busy={busyId === item.task_id}
                  readonly={readonly}
                />
              ))}
              {pendingItems.length < groups.pending.length && <Button size="small" onClick={() => setPendingLimit(value => value + NEXT_QUEUE_LIMIT)}>查看更多待处理任务</Button>}
              {candidates.length > 0 && (
                <ol className="dispatch-candidates" aria-label="候补工程">
                  {candidates.map((item, index) => (
                    <li key={item.task_id}>
                      <span className="dispatch-queue-index mono" aria-hidden="true">{String(pendingItems.length + index + 1).padStart(2, "0")}</span>
                      <span className="dispatch-candidate-name" title={item.task_name}>{item.task_name}</span>
                      {item.account_role || candidateLane ? <QuotaSlot
                        role={item.account_role || candidateLane}
                        state="等待"
                        position={candidateSlots.get(item.task_id)}
                      /> : <small>候补</small>}
                    </li>
                  ))}
                </ol>
              )}
              {showCandidates && upcoming.isError ? (
                <div className="dispatch-lane-foot" role="status">候补清单刷新失败。 <Button size="small" onClick={() => void upcoming.refetch()}>重试</Button></div>
              ) : showCandidates && upcoming.isPending ? (
                <p className="dispatch-lane-foot" role="status">正在读取候补工程…</p>
              ) : null}
              {!pendingItems.length && !candidates.length && !(showCandidates && (upcoming.isPending || upcoming.isError)) ? (
                <LaneEmpty
                  title="暂无待处理工程"
                  body={pendingQueueHint(snapshot)}
                />
              ) : null}
            </QueueLane>

            <QueueLane
              zone="processing"
              title="正在处理"
              count={groups.processing.length}
              shown={processingItems.length}
              caption="查看任务阶段和最新执行轨迹"
            >
              {processingItems.map((item, index) => (
                <QueueRow
                  key={item.task_id}
                  item={item}
                  zone="processing"
                  rank={index + 1}
                  submissionLimit={snapshot.submission_quota?.limit}
                  eta={eta.get(item.task_id)}
                  onExplain={() => setDetail({ id: item.task_id, view: "wait" })}
                  onTrace={() => setDetail({ id: item.task_id, view: "trace" })}
                  onAction={(name) => action.mutate({ item, action: name })}
                  busy={busyId === item.task_id}
                  readonly={readonly}
                />
              ))}
              {processingItems.length < groups.processing.length && <Button size="small" onClick={() => setProcessingLimit(value => value + NEXT_QUEUE_LIMIT)}>查看更多运行中任务</Button>}
              {!processingItems.length ? (
                <LaneEmpty title="当前没有运行中的任务" body="待处理任务通过启动检查后，会在这里显示进度。" />
              ) : null}
            </QueueLane>

          </div>
        </main>

        <aside className="dispatch-rail">
          <IntakePanel />
          <section className="dispatch-fold">
            <button type="button" aria-expanded={limitsOpen} aria-controls="dispatch-limits" onClick={() => setLimitsOpen(!limitsOpen)}>
              <strong>调度参数</strong><span>{Object.keys(edits).length ? "待应用" : snapshot.limits.mode === "tasks" ? "任务优先" : "容器优先"}</span><ChevronDown size={16} />
            </button>
            <div id="dispatch-limits" hidden={!limitsOpen}>
              <LimitForm snapshot={snapshot} draft={draft} setDraft={setDraft} readonly={readonly} applying={applyLimits.isPending}
                onApply={() => applyLimits.mutate(limitChanges(baseline, draft))} />
              <p className={snapshot.limits.synced ? "sync-ok" : "sync-warn"}>
                {snapshot.limits.synced ? "技能已同步" : `技能侧上限 ${snapshot.limits.skill_limit || "—"}`}
              </p>
            </div>
          </section>
          <section className="dispatch-fold">
            <button type="button" aria-expanded={monitorOpen} aria-controls="dispatch-monitor" onClick={() => setMonitorOpen(!monitorOpen)}>
              <strong>运行监控</strong><span>健康与统计</span><ChevronDown size={16} />
            </button>
            <div id="dispatch-monitor" hidden={!monitorOpen}>
              <Panel
                title="运行健康"
                actions={<Badge tone={snapshot.holds.length ? "warn" : "ok"}>{snapshot.holds.length ? `${snapshot.holds.length} 条挂起` : "无挂起"}</Badge>}
              >
                <div className="panel-body dispatch-probes">
                  <ProbeLine label="网关" ok={!snapshot.holds.some((hold) => hold.source === "llm_guard")} onTest={() => probe.mutate()} busy={probe.isPending} />
                  <ProbeLine label="Docker" ok={system.data?.docker_ok} />
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
                  <Metric label="完成" value={throughput.data?.completed ?? "—"} />
                  <Metric label="失败" value={throughput.data?.failed ?? "—"} />
                  <Metric label="容器利用率" value={throughput.data ? `${Math.round(throughput.data.container_utilization * 100)}%` : "—"} />
                  <Metric label="单题运行中位" value={formatMinutes(throughput.data?.medians_seconds.running_s ?? 0)} />
                </div>
              </Panel>

              <Panel title="会话质量">
                <div className="panel-body dispatch-metrics">
                  <Metric
                    label="继续率"
                    value={sessionStats.data ? `${Math.round(sessionStats.data.continueRate * 100)}%` : "—"}
                  />
                  <Metric
                    label="每容器小时"
                    value={sessionStats.data?.sessionsPerContainerHour.toFixed(2) ?? "—"}
                  />
                  <Metric
                    label="接受 / 含继续"
                    value={sessionStats.data ? `${sessionStats.data.accepted} / ${sessionStats.data.acceptedWithContinue}` : "—"}
                  />
                  <Metric
                    label="主要终止原因"
                    value={sessionStats.data?.abortReasons[0]?.kind ?? "—"}
                  />
                </div>
              </Panel>

            </div>
          </section>
        </aside>
      </div>

      {selected && detail && (
        <TaskDrawer item={selected} view={detail.view} onClose={() => setDetail(null)} />
      )}
    </div>
  );
}

function DispatchHeader({
  snapshot,
  onState,
  busy,
  readonly,
  onRefresh,
  refreshing,
  openingTerminals,
  onOpenTerminals,
}: {
  snapshot: DispatchQueueSnapshot;
  onState: (desired: "running" | "paused" | "draining") => void;
  busy: boolean;
  readonly: boolean;
  onRefresh: () => void;
  refreshing: boolean;
  openingTerminals: boolean;
  onOpenTerminals: () => void;
}) {
  const running = snapshot.desired_state === "running";
  return (
    <header className="dispatch-head">
      <div className="dispatch-head-title">
        <div className="dispatch-title-line">
          <h1>任务队列</h1>
          <span className="dispatch-current-profile" aria-live="polite" aria-atomic="true">
            <span>当前配置</span>
            <strong>{snapshot.active_profile_name || "名称未知"}</strong>
          </span>
          <StatusDot
            state={snapshot.effective_state === "running" ? "running" : snapshot.effective_state === "held" ? "blocked" : "idle"}
            label={DISPATCH_STATE_LABEL[snapshot.effective_state]}
            pulse={snapshot.effective_state === "running"}
          />
        </div>
        <p className="dispatch-sentence" data-state={snapshot.effective_state}>{snapshot.sentence}</p>
      </div>
      <div className="dispatch-head-actions">
        <Button size="small" variant="ghost" disabled={refreshing} onClick={onRefresh} aria-label="刷新队列" title="刷新队列"><RefreshCw size={14} /></Button>
        <Button size="small" variant="ghost" loading={openingTerminals} onClick={onOpenTerminals}>
          <SquareTerminal size={14} /> 查看全部终端
        </Button>
        {running ? (
          <Button loading={busy} disabled={readonly} onClick={() => onState("paused")}><CirclePause size={14} /> 暂停调度</Button>
        ) : (
          <Button variant="primary" loading={busy} disabled={readonly} onClick={() => onState("running")}>
            <CirclePlay size={14} /> {snapshot.desired_state === "paused" ? "继续调度" : "启动调度"}
          </Button>
        )}
        {running ? (
          <Button size="small" variant="ghost" disabled={readonly || busy} onClick={() => onState("draining")} title="不再启动新任务，在手任务跑完后停止">
            <PauseCircle size={13} /> 排空后停止
          </Button>
        ) : null}
      </div>
    </header>
  );
}

function CapacityStrip({ snapshot, inHand }: { snapshot: DispatchQueueSnapshot; inHand: number }) {
  const now = useNow();
  const pool = (name: string) => snapshot.pools[name] ?? { used: 0, limit: 0 };
  const creation = snapshot.creation;
  const containerLimit = creation?.containers.limit ?? snapshot.limits.containers;
  const containerUsed = creation?.containers.live ?? pool("containers").used;
  const taskLimit = creation?.max_tasks ?? snapshot.limits.max_tasks;
  const tasksFirst = (snapshot.limits.mode ?? "containers") === "tasks";
  const waitingContinue = snapshot.session_capacity?.waiting ?? 0;
  const cooldownLeft = creation?.next_allowed_at
    ? Math.max(0, Math.round((new Date(creation.next_allowed_at).getTime() - now) / 1000))
    : 0;
  const cooldown = creation?.cooldown_seconds ?? snapshot.limits.create_cooldown_seconds;
  const containerNote = creation
    ? `运行 ${creation.containers.live} · 待起 ${creation.containers.waiting + creation.containers.extra}`
      + (snapshot.elastic.enabled
        ? ` · 弹性 ${snapshot.elastic.current}（保底 ${snapshot.elastic.floor}）`
        : "")
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
              <i style={{ transform: `scaleX(${cell.limit ? Math.min(1, cell.used / cell.limit) : 0})` }} />
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
  candidateCount,
  caption,
  children,
}: {
  zone: QueueZone;
  title: string;
  count: number;
  shown: number;
  candidateCount?: number;
  caption: string;
  children: ReactNode;
}) {
  const hidden = Math.max(0, count - shown);
  return (
    <section className="dispatch-lane" data-zone={zone}>
      <header className="dispatch-lane-head">
        <div>
          <h2>
            {title}<b>{count}{candidateCount !== undefined && " 已排队"}</b>
            {!!candidateCount && <em>{candidateCount} 候补</em>}
          </h2>
          <p>{caption}</p>
        </div>
      </header>
      <div className="dispatch-lane-body">{children}</div>
      {hidden ? <footer className="dispatch-lane-foot">另有 {hidden} 条未展开</footer> : null}
    </section>
  );
}

function QuotaAccount({ role, account }: { role: AccountRole; account: AccountQuota | null }) {
  const label = role === "primary" ? "主号" : "副号";
  return <section className="dispatch-quota-account" aria-label={`${label}提交额度`}>
    <strong>{label}{account?.account ? ` · ${account.account}` : ""}</strong>
    <p>{account?.project ? `工程池 ${account.project}` : "工程池待配置"}</p>
    <p>已提交 {account?.submitted ?? "—"} · 在途预留 {account?.reserved ?? "—"} · 预计可用 {account?.remaining ?? "—"}</p>
    {!!account?.discarded && <p>已扣除今日废弃 {account.discarded} 条</p>}
    {account?.platform_closed && <p className="danger-text">平台已拒绝今日的新建提交</p>}
    {!!account?.stopping && <p className="danger-text">{account.stopping} 个任务正在终止</p>}
    {!!account?.stopped && <p>今日已终止并留档 {account.stopped} 个任务</p>}
    {account?.error && <p className="danger-text">{account.error}</p>}
    {account?.updated_at && <small>统计更新于 {formatDateTime(account.updated_at)}</small>}
    {!!account?.deferred?.length && <ul className="dispatch-quota-waiting" aria-label={`${label}待续交项目`}>
      {account.deferred.map((item) => <li key={item.task_id}>
        <Link className="text-link" to={`/tasks/${item.task_id}`}>{item.task_name}</Link>
        <QuotaSlot role={role} state="等待" position={item.estimated_submission} />
        <TaskSubmissionNumber id={item.task_id} estimated={item.estimated_submission} />
        <span>{item.project} · {item.reason} · {formatDateTime(item.retry_at)}</span>
      </li>)}
    </ul>}
  </section>;
}

function TaskSubmissionNumber({ id, estimated, limit }: { id: string; estimated?: number | null; limit?: number }) {
  const overLimit = estimated != null && limit != null && estimated > limit;
  return <small className={overLimit ? "danger-text" : undefined}>
    <code title={id}>任务编号 {id.slice(0, 13)}</code>
    {overLimit && " · 超出今日上限"}
  </small>;
}

function QuotaSlot({
  role,
  state,
  position,
}: {
  role?: AccountRole | "";
  state: "在途" | "等待";
  position?: number | null;
}) {
  if (!role) return null;
  const label = role === "primary" ? "主号" : "副号";
  const slot = position == null ? "" : `-${position}`;
  return <span
    className="dispatch-slot-badge"
    data-state={state === "在途" ? "active" : "waiting"}
    title={`${label}今日${position == null ? "" : `第 ${position} 个`}提交位 · ${state}`}
  >
    {label}-{state}{slot}
  </span>;
}

function pendingQueueHint({ desired_state, creation }: DispatchQueueSnapshot): string {
  if (desired_state === "paused") return "调度已暂停。继续调度后，运行中的生产计划会按可用名额补入任务。";
  if (desired_state === "draining") return "调度正在排空，已停止领取新任务。";
  if (creation && !creation.allowed && creation.text) return creation.text;
  return "运行中的生产计划会按可用名额领取工程。";
}

function LaneEmpty({ title, body }: { title: string; body: string }) {
  return (
    <div className="dispatch-lane-empty">
      <strong>{title}</strong>
      <p>{body}</p>
    </div>
  );
}

function QueueRow({
  item,
  zone,
  rank,
  submissionLimit,
  last,
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
  submissionLimit?: number;
  last?: boolean;
  eta?: string;
  onExplain: () => void;
  onTrace: () => void;
  onAction: (name: RowAction) => void;
  busy: boolean;
  readonly: boolean;
}) {
  const pending = zone === "pending";
  const until = item.wait_until || item.not_before;
  return (
    <article
      className="dispatch-queue-row"
      data-status={item.status}
      data-zone={zone}
      data-next={pending && rank === 1 || undefined}
    >
      <span className="dispatch-queue-index mono">{String(rank).padStart(2, "0")}</span>
      <div className="dispatch-queue-copy">
        <div className="dispatch-queue-status">
          <StatusDot
            state={item.active ? "running" : "queued"}
            label={STAGE_LABEL[item.stage] ?? item.stage}
            pulse={item.active}
          />
          {pending && rank === 1 ? <span>队首</span> : null}
          <QuotaSlot role={item.account_role} state="在途" position={item.estimated_submission} />
        </div>
        <Link className="dispatch-queue-name" to={`/tasks/${item.task_id}`} title={item.task_name}>
          {item.task_name}
        </Link>
        <TaskSubmissionNumber id={item.task_id} estimated={item.estimated_submission} limit={submissionLimit} />
        {zone === "processing" ? (
          <span className="dispatch-queue-note">
            更新于 <time dateTime={item.updated_at}>{formatDateTime(item.updated_at)}</time>
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
        ) : (
          <button type="button" className="dispatch-queue-reason" onClick={onExplain} title="查看等待原因">
            {item.reason}
          </button>
        )}
      </div>
      <div className="dispatch-queue-meta mono">
        {pending && until ? <span>排队重跑 <Countdown at={until} /></span> : null}
        {pending && !until && eta ? <span>预计 <Countdown at={eta} /></span> : null}
        {item.desktop_recovery_attempts ? (
          <span title={item.desktop_recovery_error || item.autopilot_detail || "无线程时重开深链接，已有线程时补按发送"}>
            <Badge tone="warn">自动重试 {item.desktop_recovery_attempts}/2</Badge>
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
        {pending ? (
          <details className="dispatch-more">
            <summary title="更多操作" aria-label="更多操作"><MoreHorizontal size={14} /></summary>
            <div>
              <button type="button" disabled={readonly || busy || rank === 1} onClick={() => onAction("pin")} title="置顶">置顶</button>
              <button type="button" disabled={readonly || busy || rank === 1} onClick={() => onAction("up")} title="上移">上移一位</button>
              <button type="button" disabled={readonly || busy || last} onClick={() => onAction("down")} title="下移">下移一位</button>
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
  snapshot: DispatchQueueSnapshot;
  draft: DispatchLimitsRequest;
  setDraft: (draft: DispatchLimitsRequest) => void;
  readonly: boolean;
  applying: boolean;
  onApply: () => void;
}) {
  const baseline = limitDraft(snapshot);
  const dirty = Object.keys(limitChanges(baseline, draft)).length > 0;
  const containers = draft.containers ?? snapshot.limits.containers;
  const maxTasks = draft.max_tasks ?? snapshot.limits.max_tasks;
  const cooldown = draft.create_cooldown_seconds ?? snapshot.limits.create_cooldown_seconds;
  const mode = draft.mode ?? snapshot.limits.mode ?? "containers";
  const tasksFirst = mode === "tasks";
  const elastic = draft.elastic ?? snapshot.limits.elastic;
  const elasticEnabled = elastic.enabled ?? false;
  const elasticCeiling = elastic.ceiling ?? 8;
  const elasticStatus = snapshot.elastic;
  const inRange = (n: number, min: number, max: number) => Number.isInteger(n) && n >= min && n <= max;
  const valid = inRange(containers, 1, 8) && inRange(maxTasks, 1, 50) && inRange(cooldown, 20, 3600)
    && (!elasticEnabled || inRange(elasticCeiling, containers, 8));
  return (
    <fieldset className="dispatch-limit-form panel-body" disabled={readonly || applying}>
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
        onChange={(value) => setDraft({
          ...draft,
          containers: value,
          elastic: { ...elastic, ceiling: Number.isFinite(value) ? Math.max(value, elasticCeiling) : elasticCeiling },
        })}
      />
      <label className="dispatch-limit-row dispatch-mode-row" title="容器满且有任务等待时逐级扩容；429 重试等待超过阈值时自动缩容">
        <span>弹性扩容</span>
        <span className="dispatch-limit-inline">
          <input
            type="checkbox"
            aria-label="启用弹性扩容"
            checked={elasticEnabled}
            onChange={(event) => setDraft({
              ...draft,
              elastic: {
                ...elastic,
                enabled: event.target.checked,
                ceiling: Math.max(containers, elasticCeiling),
              },
            })}
          />
          <small>{elasticEnabled ? "已启用" : "关闭"}</small>
        </span>
      </label>
      <LimitNumber
        label="弹性上限"
        hint="弹性扩容的最高容器数；最大容器数是保底值"
        value={elasticCeiling}
        min={containers}
        max={8}
        inactive={!elasticEnabled}
        inactiveLabel="关闭"
        onChange={(value) => setDraft({ ...draft, elastic: { ...elastic, ceiling: value } })}
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
      <p className="dispatch-limit-note dispatch-elastic-note">
        {elasticEnabled
          ? `当前生效 ${elasticStatus.current}/${elasticStatus.ceiling}，保底 ${elasticStatus.floor}；近 5 分钟 429 ${elasticStatus.recent_rate_limits} 次、等待占比 ${elasticStatus.stall_percent_now}%${elasticStatus.learned_for_hour ? `，本时段常用 ${elasticStatus.learned_for_hour}` : ""}。${elasticStatus.last_reason || "等待满负载信号"}`
          : "弹性关闭：容器上限固定为最大容器数，不会自动增减。"}
      </p>
      <div className="dispatch-limit-actions" data-dirty={dirty || undefined}>
        <span aria-live="polite">{!valid ? "请按范围填写整数" : dirty ? "有未应用的修改" : ""}</span>
        <Button size="small" variant="ghost" disabled={!dirty} onClick={() => setDraft(baseline)}>还原</Button>
        <Button variant="primary" size="small" disabled={!dirty || !valid} loading={applying} onClick={onApply}>应用</Button>
      </div>
    </fieldset>
  );
}

function TaskDrawer({ item, view, onClose }: { item: DispatchQueueItem; view: "wait" | "trace"; onClose: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const node = dialog.current;
    const previous = document.activeElement;
    node?.showModal();
    return () => {
      node?.close();
      if (previous instanceof HTMLElement) previous.focus();
    };
  }, []);
  const trace = view === "trace";
  const title = trace ? "执行轨迹" : "等待原因";
  return (
    <dialog ref={dialog} className={`dispatch-drawer${trace ? " dispatch-trace-drawer" : ""}`}
      aria-label={`${title} · ${item.task_name}`} onCancel={(event) => { event.preventDefault(); onClose(); }}
      onClick={(event) => {
        if (event.target === dialog.current && event.clientX < dialog.current.getBoundingClientRect().left) onClose();
      }}>
      <header>
        <div><span className="eyebrow">{title}</span><h2>{item.task_name}</h2></div>
        <div className="dispatch-trace-head-actions">
          {trace && <Link to={`/tasks/${item.task_id}?tab=timeline`}>完整任务页</Link>}
          <Button size="small" variant="ghost" onClick={onClose} aria-label="关闭详情"><X size={14} /></Button>
        </div>
      </header>
      {trace ? <CoreTracePanel taskId={item.task_id} /> : <>
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
      </>}
    </dialog>
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
  inactiveLabel = "未启用",
  onChange,
}: {
  label: string;
  value: number;
  min: number;
  max: number;
  suffix?: string;
  hint?: string;
  inactive?: boolean;
  inactiveLabel?: string;
  onChange: (value: number) => void;
}) {
  return (
    <label className="dispatch-limit-row" title={hint} data-inactive={inactive || undefined}>
      <span>{label}</span>
      <input className="compact-number" type="number" aria-label={label} min={min} max={max} disabled={inactive} title={`${min}–${max}`} value={Number.isNaN(value) ? "" : value} onChange={(event) => onChange(event.target.valueAsNumber)} />
      <small>{inactive ? inactiveLabel : suffix ?? ""}</small>
    </label>
  );
}

function ProbeLine({ label, ok, value, onTest, busy }: { label: string; ok?: boolean; value?: string; onTest?: () => void; busy?: boolean }) {
  return (
    <div className="dispatch-probe-line">
      <StatusDot state={ok === undefined ? "idle" : ok ? "done" : "blocked"} label={label} />
      <span>{value ?? (ok === undefined ? "待确认" : ok ? "正常" : "异常")}</span>
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

function groupQueue(queue: DispatchQueueItem[]): Record<QueueZone, DispatchQueueItem[]> {
  const groups: Record<QueueZone, DispatchQueueItem[]> = { pending: [], processing: [] };
  for (const item of queue) {
    if (HIDDEN_STATUSES.has(item.status)) continue;
    groups[item.active ? "processing" : "pending"].push(item);
  }
  for (const items of Object.values(groups)) {
    items.sort((a, b) => (a.queue_position ?? Infinity) - (b.queue_position ?? Infinity));
  }
  return groups;
}

function limitDraft(snapshot: DispatchQueueSnapshot): DispatchLimitsRequest {
  return {
    mode: snapshot.limits.mode ?? "containers",
    containers: snapshot.limits.containers,
    elastic: {
      enabled: snapshot.limits.elastic.enabled,
      ceiling: snapshot.limits.elastic.ceiling,
    },
    max_tasks: snapshot.limits.max_tasks,
    create_cooldown_seconds: snapshot.limits.create_cooldown_seconds,
  };
}

function limitChanges(baseline: DispatchLimitsRequest, next: DispatchLimitsRequest): DispatchLimitsRequest {
  const changes: DispatchLimitsRequest = {};
  if (next.mode !== baseline.mode) changes.mode = next.mode;
  for (const key of ["containers", "max_tasks", "create_cooldown_seconds"] as const) {
    if (next[key] !== baseline[key]) changes[key] = next[key];
  }
  const elastic: DispatchLimitsRequest["elastic"] = {};
  if (next.elastic?.enabled !== baseline.elastic?.enabled) elastic.enabled = next.elastic?.enabled;
  if (next.elastic?.ceiling !== baseline.elastic?.ceiling) elastic.ceiling = next.elastic?.ceiling;
  if (Object.keys(elastic).length) changes.elastic = elastic;
  return changes;
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
