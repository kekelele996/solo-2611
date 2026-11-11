import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowDown,
  ArrowUp,
  CirclePause,
  CirclePlay,
  ListRestart,
  LockKeyhole,
  PauseCircle,
  Pin,
  RefreshCw,
  ShieldCheck,
  Sparkles,
  Trash2,
} from "lucide-react";
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { api } from "../api/client";
import type { DispatchHold, DispatchLimitsRequest, DispatchQueueItem, DispatchSnapshot } from "../api/types";
import { useToast } from "../components/toast-context";
import { Badge, Button, EmptyState, ErrorPanel, LoadingLines, Panel, StatusDot } from "../components/ui";
import { errorText } from "../utils/display";
import { formatDateTime } from "../utils/time";

type QueueTab = "all" | "needs_action" | "active" | "waiting" | "finished";

const PRESETS: Array<{ value: DispatchSnapshot["preset"]; label: string; hint: string }> = [
  { value: "throughput", label: "跑满", hint: "容器优先，在手自适应" },
  { value: "balanced", label: "均衡", hint: "日常推荐" },
  { value: "careful", label: "稳妥", hint: "一进一出" },
  { value: "custom", label: "自定义", hint: "手填派生参数" },
];

const STATE_LABEL: Record<DispatchSnapshot["effective_state"], string> = {
  running: "运行中",
  paused: "已暂停",
  draining: "排空中",
  held: "已自动挂起",
  degraded: "部分受限",
  idle: "空闲",
};

export function DispatchPage() {
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const [tab, setTab] = useState<QueueTab>("all");
  const [draft, setDraft] = useState<DispatchLimitsRequest | null>(null);
  const [explainFor, setExplainFor] = useState<DispatchQueueItem | null>(null);
  const [, setClockTick] = useState(0);
  const dispatch = useQuery({
    queryKey: ["dispatch"],
    queryFn: api.dispatch,
    refetchInterval: 2500,
  });

  useEffect(() => {
    const timer = window.setInterval(() => setClockTick((value) => value + 1), 1000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    const snapshot = dispatch.data;
    if (!snapshot || draft) return;
    setDraft(limitDraft(snapshot));
  }, [dispatch.data, draft]);

  const state = useMutation({
    mutationFn: (desired: "running" | "paused" | "draining") => api.dispatchState(desired),
    onSuccess: (_, desired) => {
      toast(
        desired === "running" ? "调度已启动" : desired === "draining" ? "已进入排空后停止" : "调度已暂停",
        desired === "running" ? "ok" : "info",
      );
      setDraft(null);
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
      void queryClient.invalidateQueries();
    },
    onError: (error) => toast(errorText(error), "error"),
  });
  const action = useMutation({
    mutationFn: ({ item, action }: { item: DispatchQueueItem; action: "pin" | "retry" | "quarantine" | "release" | "keep" | "up" | "down" }) =>
      action === "up" || action === "down"
        ? api.dispatchQueueAction(item.task_id, "move", action)
        : api.dispatchQueueAction(item.task_id, action),
    onSuccess: () => {
      toast("队列已更新", "ok");
      void queryClient.invalidateQueries();
    },
    onError: (error) => toast(errorText(error), "error"),
  });
  const holdAction = useMutation({
    mutationFn: async ({ hold, action }: { hold: DispatchHold; action: "probe" | "release" }) => {
      if (action === "probe") return api.dispatchProbeHold(hold.id);
      const released = await api.dispatchReleaseHold(hold.id);
      return {
        source: released.source,
        ok: released.ok,
        message: "挂起已解除；相关残留会自动重新排队",
        detail: {},
      };
    },
    onSuccess: (result) => {
      toast(result.message, result.ok ? "ok" : "warn");
      void queryClient.invalidateQueries();
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
      void queryClient.invalidateQueries();
    },
    onError: (error) => toast(errorText(error), "error"),
  });

  if (dispatch.isPending) return <div className="page"><LoadingLines rows={10} /></div>;
  if (dispatch.isError) {
    return <div className="page"><ErrorPanel error={dispatch.error} onRetry={() => void dispatch.refetch()} /></div>;
  }
  const snapshot = dispatch.data;
  const readonly = snapshot.engine === "v2";
  const visible = filterQueue(snapshot, tab);
  const limitsDirty = Boolean(draft && JSON.stringify(draft) !== JSON.stringify(limitDraft(snapshot)));
  const targetValue = [snapshot.plan.target, snapshot.plan.daily_claim_cap].find(
    (value): value is string | number => typeof value === "string" || typeof value === "number",
  );
  const planMode = typeof snapshot.plan.mode === "string" ? snapshot.plan.mode : "当前未下达";

  return (
    <div className="page dispatch-page">
      <DispatchHeader
        snapshot={snapshot}
        onState={(desired) => state.mutate(desired)}
        busy={state.isPending}
        readonly={readonly}
        onPreset={(preset) =>
          applyLimits.mutate({
            preset,
            containers: snapshot.limits.containers,
            in_hand: preset === "custom" ? snapshot.limits.in_hand : 0,
            sessions: preset === "custom" ? snapshot.limits.sessions : 0,
          })
        }
      />
      {readonly ? (
        <div className="system-banner" role="status">
          当前为 v2 调度引擎，调度页只读。将 <code>[dispatch] engine</code> 改为 <code>"v3"</code> 并重启后端即可切换；
          原生产线与全部 v2 数据保持可用。
        </div>
      ) : null}

      {snapshot.holds.length ? (
        <section className="dispatch-holds" aria-label="当前挂起">
          {snapshot.holds.map((hold) => (
            <div className="dispatch-hold" key={hold.id} data-auto={hold.auto}>
              <LockKeyhole size={16} />
              <div>
                <strong>{hold.reason}</strong>
                <span>
                  范围 {hold.scope.join("、")} · {hold.next_at ? `下次探测 ${formatCountdown(hold.next_at)}` : "需要你处理"}
                  {hold.last_error ? ` · ${hold.last_error}` : ""}
                </span>
              </div>
              <Button size="small" variant="ghost" loading={holdAction.isPending} onClick={() => holdAction.mutate({ hold, action: "probe" })}>
                立即探测
              </Button>
              <Button size="small" onClick={() => holdAction.mutate({ hold, action: "release" })}>解除</Button>
            </div>
          ))}
        </section>
      ) : null}

      <div className="dispatch-layout">
        <main className="dispatch-main">
          <section className="dispatch-toolbar">
            <div className="dispatch-tabs" role="tablist" aria-label="调度队列">
              {[
                ["all", "全部", snapshot.counts.all],
                ["needs_action", "需处理", snapshot.counts.needs_action],
                ["active", "执行中", snapshot.counts.active],
                ["waiting", "等待", snapshot.counts.waiting],
                ["finished", "已结束", snapshot.counts.finished],
              ].map(([value, label, count]) => (
                <button
                  type="button"
                  role="tab"
                  aria-selected={tab === value}
                  key={String(value)}
                  data-active={tab === value || undefined}
                  onClick={() => setTab(value as QueueTab)}
                >
                  {label}<span>{count}</span>
                </button>
              ))}
            </div>
            <div className="row-actions">
              <Button size="small" variant="ghost" onClick={() => void dispatch.refetch()}>
                <RefreshCw size={13} /> 刷新
              </Button>
              {snapshot.counts.finished ? (
                <Button
                  size="small"
                  variant="ghost"
                  onClick={() => {
                    void api.dispatchClearFinished().then(() => queryClient.invalidateQueries());
                  }}
                >
                  <Trash2 size={13} /> 清理已结束
                </Button>
              ) : null}
            </div>
          </section>

          {visible.length ? (
            <div className="dispatch-queue">
              {visible.map((item) => (
                <QueueRow
                  key={item.task_id}
                  item={item}
                  onExplain={() => setExplainFor(item)}
                  onAction={(name) => action.mutate({ item, action: name })}
                  busy={action.isPending && action.variables?.item.task_id === item.task_id}
                  readonly={readonly}
                />
              ))}
            </div>
          ) : (
            <EmptyState
              icon={<Sparkles size={28} />}
              title={tab === "all" ? "调度队列为空。可以从任务页加入任务，或下达生产目标。" : "这个分组暂时没有任务。"}
            />
          )}

          <Panel title="接下来启动" className="dispatch-upcoming">
            {snapshot.upcoming.length ? (
              <ol>
                {snapshot.upcoming.map((item) => (
                  <li key={item.task_id}>
                    <span className="mono">{item.queue_position}</span>
                    <Link to={`/tasks/${item.task_id}`}>{item.task_name}</Link>
                    <span>{item.reason}</span>
                    <time>{formatCountdown(item.estimated_start_at)}</time>
                  </li>
                ))}
              </ol>
            ) : (
              <p className="muted">当前没有等待启动的任务。</p>
            )}
          </Panel>
        </main>

        <aside className="dispatch-rail">
          <Panel
            title="调度上限"
            actions={
              <span className={snapshot.limits.synced ? "sync-ok" : "sync-warn"}>
                {snapshot.limits.synced ? "已同步 ✓" : `技能侧 ${snapshot.limits.skill_limit || "—"}`}
              </span>
            }
          >
            {draft ? (
              <fieldset className="dispatch-limit-form" disabled={readonly}>
                <LimitNumber label="容器数" value={draft.containers ?? snapshot.limits.containers} min={1} max={8} onChange={(containers) => setDraft({ ...draft, containers })} />
                <div className="dispatch-limit-row">
                  <span>弹性扩容</span>
                  <label className="check-inline">
                    <input
                      type="checkbox"
                      checked={Boolean(draft.elastic?.enabled)}
                      onChange={(event) => setDraft({ ...draft, elastic: { ...draft.elastic, enabled: event.target.checked } })}
                    />
                    开启
                  </label>
                  <input
                    className="compact-number"
                    type="number"
                    min={draft.containers ?? 1}
                    max={8}
                    value={draft.elastic?.ceiling ?? snapshot.elastic.ceiling}
                    onChange={(event) => setDraft({ ...draft, elastic: { ...draft.elastic, ceiling: Number(event.target.value) } })}
                    aria-label="弹性上限"
                  />
                  <small>生效 {snapshot.elastic.effective}</small>
                </div>
                <AutoLimit
                  label="会话并发"
                  value={draft.sessions ?? 0}
                  autoValue={snapshot.limits.sessions}
                  onChange={(sessions) => setDraft({ ...draft, sessions })}
                />
                <AutoLimit
                  label="在手上限"
                  value={draft.in_hand ?? 0}
                  autoValue={snapshot.limits.in_hand}
                  onChange={(in_hand) => setDraft({ ...draft, in_hand })}
                />
                <LimitNumber label="启动间隔" suffix="秒" value={draft.launch_spacing_seconds ?? 45} min={10} max={3600} onChange={(launch_spacing_seconds) => setDraft({ ...draft, launch_spacing_seconds })} />
                <LimitNumber label="录屏并发" value={draft.recording ?? 1} min={1} max={16} onChange={(recording) => setDraft({ ...draft, recording })} />
                <div className="dispatch-limit-actions">
                  <Button size="small" variant="ghost" disabled={!limitsDirty} onClick={() => setDraft(limitDraft(snapshot))}>还原</Button>
                  <Button
                    variant="primary"
                    size="small"
                    disabled={!limitsDirty}
                    loading={applyLimits.isPending}
                    onClick={() => applyLimits.mutate({ ...draft, preset: "custom" })}
                  >
                    应用
                  </Button>
                </div>
              </fieldset>
            ) : null}
          </Panel>

          <Panel title="生产目标">
            <div className="dispatch-plan">
              <span className="dispatch-plan-progress">
                {targetValue || "未设置"}
              </span>
              <p>
                {planMode} · 在手 {snapshot.pools.containers?.used ?? 0}/{snapshot.limits.in_hand}
              </p>
              <Link className="text-link" to="/source">编辑项目与权重 →</Link>
            </div>
          </Panel>

          <Panel title="自动启停" actions={<Badge tone={snapshot.holds.length ? "warn" : "ok"}>{snapshot.holds.length ? `${snapshot.holds.length} 条挂起` : "正常"}</Badge>}>
            <div className="dispatch-probes">
              <ProbeLine label="网关" ok={!snapshot.holds.some((hold) => hold.source === "llm_guard")} onTest={() => probe.mutate()} busy={probe.isPending} />
              <ProbeLine label="Docker" ok={Boolean((snapshot.pools.containers?.limit || 0) > 0 && snapshot.effective_state !== "held")} />
              <ProbeLine label="守护" ok={snapshot.guard.mode !== "off"} value={`${snapshot.guard.mode} · 命中 ${snapshot.guard.count}`} />
            </div>
            <Button size="small" variant="ghost" loading={recover.isPending} onClick={() => recover.mutate()}>
              <ShieldCheck size={13} /> 清理挂起期间残留
            </Button>
          </Panel>

          <Panel title="吞吐" actions={<span className="muted">24h</span>}>
            <div className="dispatch-metrics">
              <Metric label="完成" value={snapshot.throughput.completed} />
              <Metric label="隔离" value={snapshot.throughput.quarantined} />
              <Metric label="利用率" value={`${Math.round(snapshot.throughput.container_utilization * 100)}%`} />
              <Metric label="单题中位" value={formatMinutes(snapshot.throughput.medians_seconds.running_s ?? 0)} />
            </div>
          </Panel>

          <Panel title="兜底策略" actions={<span className="muted">{snapshot.guard.mode === "enforce" ? "自动处置" : "仅观察"}</span>}>
            {snapshot.guard.hits.length ? (
              <ul className="dispatch-guard-list">
                {snapshot.guard.hits.map((hit) => (
                  <li key={String(hit.id)}>
                    <strong>{String(hit.kind)}</strong>
                    <span>{String(hit.reason)}</span>
                  </li>
                ))}
              </ul>
            ) : <p className="muted">没有新的守护命中。</p>}
          </Panel>
        </aside>
      </div>

      {explainFor ? <ExplainDrawer item={explainFor} onClose={() => setExplainFor(null)} /> : null}
    </div>
  );
}

function DispatchHeader({
  snapshot,
  onState,
  busy,
  readonly,
  onPreset,
}: {
  snapshot: DispatchSnapshot;
  onState: (desired: "running" | "paused" | "draining") => void;
  busy: boolean;
  readonly: boolean;
  onPreset: (preset: DispatchSnapshot["preset"]) => void;
}) {
  return (
    <header className="dispatch-head">
      <div>
        <span className="eyebrow">DISPATCH CENTER / ONE GATE</span>
        <div className="dispatch-title-line">
          <h1>调度</h1>
          <StatusDot state={snapshot.effective_state === "running" ? "running" : snapshot.effective_state === "held" ? "blocked" : "idle"} label={STATE_LABEL[snapshot.effective_state]} pulse={snapshot.effective_state === "running"} />
        </div>
        <p className="dispatch-sentence" data-state={snapshot.effective_state}>{snapshot.sentence}</p>
      </div>
      <div className="dispatch-head-actions">
        <select aria-label="调度预设" disabled={readonly} value={snapshot.preset} onChange={(event) => onPreset(event.target.value as DispatchSnapshot["preset"])}>
          {PRESETS.map((preset) => <option key={preset.value} value={preset.value}>{preset.label} · {preset.hint}</option>)}
        </select>
        {snapshot.desired_state === "running" ? (
          <Button loading={busy} disabled={readonly} onClick={() => onState("paused")}><CirclePause size={14} /> 暂停</Button>
        ) : (
          <Button variant="primary" loading={busy} disabled={readonly} onClick={() => onState("running")}><CirclePlay size={14} /> {snapshot.desired_state === "paused" ? "继续" : "启动"}</Button>
        )}
        <Button size="small" variant="ghost" loading={busy} disabled={readonly} onClick={() => onState("draining")}>
          <PauseCircle size={13} /> 排空后停止
        </Button>
      </div>
    </header>
  );
}

function QueueRow({
  item,
  onExplain,
  onAction,
  busy,
  readonly,
}: {
  item: DispatchQueueItem;
  onExplain: () => void;
  onAction: (name: "pin" | "retry" | "quarantine" | "release" | "keep" | "up" | "down") => void;
  busy: boolean;
  readonly: boolean;
}) {
  const needs = item.status === "quarantined";
  return (
    <article className="dispatch-row" data-status={item.status} data-keep={item.keep || undefined}>
      <div className="dispatch-row-status">
        <StatusDot
          state={item.active ? "running" : needs ? "blocked" : item.status === "done" ? "exported" : "queued"}
          label={needs ? "需处理" : item.active ? "执行中" : item.status === "done" ? "已结束" : "等待中"}
          pulse={item.active}
        />
        <span className="mono">{item.stage}</span>
      </div>
      <Link className="dispatch-row-name" to={`/tasks/${item.task_id}`}>{item.task_name}</Link>
      <button type="button" className="dispatch-reason" onClick={onExplain}>
        {item.reason}
        <small>{item.queue_position ? `#${item.queue_position}` : item.wait_code || "查看门禁"}</small>
      </button>
      <span className="mono dispatch-attempts">{item.total_attempts ? `尝试 ${item.total_attempts}` : "首次"}</span>
      <div className="dispatch-row-actions">
        {!item.active ? (
          <>
            <Button size="small" variant="ghost" disabled={readonly || busy} onClick={() => onAction("up")} title="上移"><ArrowUp size={12} /></Button>
            <Button size="small" variant="ghost" disabled={readonly || busy} onClick={() => onAction("down")} title="下移"><ArrowDown size={12} /></Button>
            <Button size="small" variant="ghost" disabled={readonly || busy} onClick={() => onAction("pin")} title="置顶"><Pin size={12} /></Button>
          </>
        ) : null}
        {needs || item.status === "backoff" ? (
          <Button size="small" loading={busy} disabled={readonly} onClick={() => onAction("retry")}><ListRestart size={12} /> 重试</Button>
        ) : null}
        {!item.active ? (
          <Button size="small" variant="ghost" disabled={readonly || busy} onClick={() => onAction("quarantine")}>隔离</Button>
        ) : null}
        <Button size="small" variant="ghost" disabled={readonly || busy} onClick={() => onAction("keep")}>{item.keep ? "解除保留" : "保留"}</Button>
      </div>
    </article>
  );
}

function ExplainDrawer({ item, onClose }: { item: DispatchQueueItem; onClose: () => void }) {
  return (
    <div className="dispatch-drawer-layer" onMouseDown={onClose}>
      <aside className="dispatch-drawer" onMouseDown={(event) => event.stopPropagation()}>
        <header>
          <div>
            <span className="eyebrow">WHY NOT RUNNING</span>
            <h2>{item.task_name}</h2>
          </div>
          <Button size="small" variant="ghost" onClick={onClose}>关闭</Button>
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

function LimitNumber({ label, value, min, max, suffix, onChange }: { label: string; value: number; min: number; max: number; suffix?: string; onChange: (value: number) => void }) {
  return (
    <label className="dispatch-limit-row">
      <span>{label}</span>
      <input className="compact-number" type="number" min={min} max={max} value={value} onChange={(event) => onChange(Number(event.target.value))} />
      {suffix ? <small>{suffix}</small> : null}
    </label>
  );
}

function AutoLimit({ label, value, autoValue, onChange }: { label: string; value: number; autoValue: number; onChange: (value: number) => void }) {
  return (
    <label className="dispatch-limit-row">
      <span>{label}</span>
      <input className="compact-number" type="number" min={0} max={100} value={value} onChange={(event) => onChange(Number(event.target.value))} />
      <small className={value ? "" : "auto-chip"}>{value ? "手填" : `自动 ${autoValue}`}</small>
    </label>
  );
}

function ProbeLine({ label, ok, value, onTest, busy }: { label: string; ok: boolean; value?: string; onTest?: () => void; busy?: boolean }) {
  return (
    <div className="dispatch-probe-line">
      <StatusDot state={ok ? "done" : "blocked"} label={label} />
      <span>{value ?? (ok ? "正常" : "异常")}</span>
      {onTest ? <Button size="small" variant="ghost" loading={busy} onClick={onTest}>测试</Button> : null}
    </div>
  );
}

function Metric({ label, value }: { label: string; value: number | string }) {
  return <div><span>{label}</span><strong>{value}</strong></div>;
}

function limitDraft(snapshot: DispatchSnapshot): DispatchLimitsRequest {
  return {
    preset: snapshot.preset,
    containers: snapshot.limits.containers,
    sessions: snapshot.settings.sessions as number,
    authoring: snapshot.settings.authoring as number,
    delivering: snapshot.settings.delivering as number,
    recording: snapshot.limits.recording,
    in_hand: snapshot.settings.in_hand as number,
    launch_spacing_seconds: snapshot.limits.launch_spacing_seconds,
    session_spacing_seconds: snapshot.settings.session_spacing_seconds as number,
    keep_awake: snapshot.keep_awake,
    elastic: {
      enabled: snapshot.elastic.enabled,
      ceiling: snapshot.elastic.ceiling,
    },
  };
}

function filterQueue(snapshot: DispatchSnapshot, tab: QueueTab): DispatchQueueItem[] {
  if (tab === "all") return snapshot.queue;
  if (tab === "needs_action") return snapshot.queue.filter((item) => item.status === "quarantined");
  if (tab === "active") return snapshot.queue.filter((item) => item.active);
  if (tab === "waiting") return snapshot.queue.filter((item) => ["queued", "backoff", "interrupted"].includes(item.status));
  return snapshot.queue.filter((item) => ["done", "failed", "removed", "awaiting_audit"].includes(item.status));
}

function formatCountdown(value: string): string {
  if (!value) return "—";
  const delta = Math.max(0, Math.round((new Date(value).getTime() - Date.now()) / 1000));
  if (delta < 60) return `${delta} 秒后`;
  if (delta < 3600) return `${Math.ceil(delta / 60)} 分钟后`;
  return formatDateTime(value);
}

function formatMinutes(seconds: number): string {
  if (!seconds) return "—";
  return `${Math.max(1, Math.round(seconds / 60))}′`;
}
