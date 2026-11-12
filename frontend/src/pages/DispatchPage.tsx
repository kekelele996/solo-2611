import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowDown,
  ArrowUp,
  CirclePause,
  CirclePlay,
  ListRestart,
  LockKeyhole,
  MoreHorizontal,
  PauseCircle,
  Pin,
  RefreshCw,
  ShieldCheck,
  Sparkles,
  Trash2,
  X,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { Link } from "react-router-dom";

import { api } from "../api/client";
import type { DispatchHold, DispatchLimitsRequest, DispatchQueueItem, DispatchSnapshot } from "../api/types";
import { IntakePanel } from "../components/IntakePanel";
import { useToast } from "../components/toast-context";
import { Badge, Button, EmptyState, ErrorPanel, LoadingLines, Panel, StatusDot } from "../components/ui";
import { errorText, formatDuration } from "../utils/display";
import { formatDateTime } from "../utils/time";

type QueueTab = "all" | "needs_action" | "active" | "waiting" | "finished";
type Group = Exclude<QueueTab, "all">;
type RowAction = "pin" | "retry" | "quarantine" | "release" | "keep" | "up" | "down";

const PRESETS: Array<{ value: DispatchSnapshot["preset"]; label: string; hint: string }> = [
  { value: "throughput", label: "跑满", hint: "容器优先，在手自适应" },
  { value: "balanced", label: "均衡", hint: "日常推荐" },
  { value: "careful", label: "稳妥", hint: "一进一出" },
  { value: "custom", label: "自定义", hint: "按右侧手填参数" },
];

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

const GROUPS: Array<{ key: Group; label: string }> = [
  { key: "needs_action", label: "需处理" },
  { key: "active", label: "执行中" },
  { key: "waiting", label: "等待启动" },
  { key: "finished", label: "已结束" },
];

const FINISHED = new Set(["done", "failed", "removed", "awaiting_audit"]);
// 与后端 IN_HAND 保持一致：需人工处理和已结束的任务不占在手。
const IN_HAND = new Set(["queued", "launching", "active", "backoff", "interrupted"]);

export function DispatchPage() {
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const [tab, setTab] = useState<QueueTab>("all");
  const [draft, setDraft] = useState<DispatchLimitsRequest | null>(null);
  const [explainFor, setExplainFor] = useState<DispatchQueueItem | null>(null);
  // 与顶栏共用 ["dispatch"] 缓存，同一份快照只请求一次。
  const dispatch = useQuery({
    queryKey: ["dispatch"],
    queryFn: api.dispatch,
    refetchInterval: 2500,
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
  const shown = tab === "all" ? GROUPS.filter((group) => groups[group.key].length) : GROUPS.filter((group) => group.key === tab);
  const total = shown.reduce((sum, group) => sum + groups[group.key].length, 0);

  return (
    <div className="page dispatch-page">
      <DispatchHeader
        snapshot={snapshot}
        onState={(desired) => state.mutate(desired)}
        busy={state.isPending}
        presetBusy={applyLimits.isPending}
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

      <CapacityStrip snapshot={snapshot} inHand={snapshot.queue.filter((item) => IN_HAND.has(item.status)).length} />

      {snapshot.holds.length ? (
        <section className="dispatch-holds" aria-label="当前挂起">
          {snapshot.holds.map((hold) => (
            <div className="dispatch-hold" key={hold.id} data-auto={hold.auto}>
              <LockKeyhole size={16} />
              <div>
                <strong>{hold.reason}</strong>
                <span>
                  影响 {hold.scope.join("、")} ·{" "}
                  {hold.next_at ? <>下次探测 <Countdown at={hold.next_at} /></> : "需要你处理"}
                  {hold.last_error ? ` · ${hold.last_error}` : ""}
                </span>
              </div>
              <Button
                size="small"
                variant="ghost"
                loading={holdAction.isPending && holdAction.variables?.hold.id === hold.id && holdAction.variables.action === "probe"}
                onClick={() => holdAction.mutate({ hold, action: "probe" })}
              >
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
              {(
                [
                  ["all", "全部", snapshot.queue.length],
                  ["needs_action", "需处理", groups.needs_action.length],
                  ["active", "执行中", groups.active.length],
                  ["waiting", "等待", groups.waiting.length],
                  ["finished", "已结束", groups.finished.length],
                ] as Array<[QueueTab, string, number]>
              ).map(([value, label, count]) => (
                <button
                  type="button"
                  role="tab"
                  aria-selected={tab === value}
                  key={value}
                  data-active={tab === value || undefined}
                  data-alert={value === "needs_action" && count > 0 ? true : undefined}
                  onClick={() => setTab(value)}
                >
                  {label}<span>{count}</span>
                </button>
              ))}
            </div>
            <div className="row-actions">
              {groups.finished.length ? (
                <Button size="small" variant="ghost" loading={clearFinished.isPending} disabled={readonly} onClick={() => clearFinished.mutate()}>
                  <Trash2 size={13} /> 清理已结束
                </Button>
              ) : null}
              <Button size="small" variant="ghost" onClick={() => void dispatch.refetch()} title="立即刷新">
                <RefreshCw size={13} />
              </Button>
            </div>
          </section>

          {total ? (
            <div className="dispatch-queue">
              {shown.map((group) => (
                <section key={group.key} className="dispatch-group" data-group={group.key}>
                  {tab === "all" ? (
                    <header className="dispatch-group-head">
                      <strong>{group.label}</strong>
                      <span>{groups[group.key].length}</span>
                    </header>
                  ) : null}
                  {groups[group.key].map((item) => (
                    <QueueRow
                      key={item.task_id}
                      item={item}
                      group={group.key}
                      eta={eta.get(item.task_id)}
                      onExplain={() => setExplainFor(item)}
                      onAction={(name) => action.mutate({ item, action: name })}
                      busy={busyId === item.task_id}
                      readonly={readonly}
                    />
                  ))}
                </section>
              ))}
            </div>
          ) : (
            <EmptyState
              icon={<Sparkles size={28} />}
              title={tab === "all" ? "调度队列为空。可以从任务页加入任务，或在题源页下达生产目标。" : "这个分组暂时没有任务。"}
            />
          )}
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
                onApply={() => applyLimits.mutate({ ...draft, preset: "custom" })}
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
              <Metric label="失败 / 隔离" value={`${snapshot.throughput.failed} / ${snapshot.throughput.quarantined}`} />
              <Metric label="容器利用率" value={`${Math.round(snapshot.throughput.container_utilization * 100)}%`} />
              <Metric label="单题运行中位" value={formatMinutes(snapshot.throughput.medians_seconds.running_s ?? 0)} />
            </div>
          </Panel>

          <IntakePanel />
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
  presetBusy,
  readonly,
  onPreset,
}: {
  snapshot: DispatchSnapshot;
  onState: (desired: "running" | "paused" | "draining") => void;
  busy: boolean;
  presetBusy: boolean;
  readonly: boolean;
  onPreset: (preset: DispatchSnapshot["preset"]) => void;
}) {
  const running = snapshot.desired_state === "running";
  return (
    <header className="dispatch-head">
      <div className="dispatch-head-title">
        <div className="dispatch-title-line">
          <h1>调度</h1>
          <StatusDot
            state={snapshot.effective_state === "running" ? "running" : snapshot.effective_state === "held" ? "blocked" : "idle"}
            label={STATE_LABEL[snapshot.effective_state]}
            pulse={snapshot.effective_state === "running"}
          />
        </div>
        <p className="dispatch-sentence" data-state={snapshot.effective_state}>{snapshot.sentence}</p>
      </div>
      <div className="dispatch-head-actions">
        <div className="dispatch-presets" role="radiogroup" aria-label="调度预设" aria-busy={presetBusy || undefined}>
          {PRESETS.map((preset) => (
            <button
              type="button"
              role="radio"
              key={preset.value}
              aria-checked={snapshot.preset === preset.value}
              data-active={snapshot.preset === preset.value || undefined}
              disabled={readonly || presetBusy}
              title={preset.hint}
              onClick={() => snapshot.preset !== preset.value && onPreset(preset.value)}
            >
              {preset.label}
            </button>
          ))}
        </div>
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
  const pool = (name: string) => snapshot.pools[name] ?? { used: 0, limit: 0 };
  const containers = pool("containers");
  const cells: Array<{ label: string; used: number; limit: number; note?: string; major?: boolean }> = [
    {
      label: "容器",
      used: containers.used,
      limit: containers.limit,
      major: true,
      note: snapshot.elastic.enabled ? `弹性 ${snapshot.elastic.floor}–${snapshot.elastic.ceiling}` : undefined,
    },
    { label: "会话", used: pool("sessions").used, limit: pool("sessions").limit, major: true },
    { label: "在手", used: inHand, limit: snapshot.limits.in_hand, major: true },
    { label: "起草", used: pool("authoring").used, limit: pool("authoring").limit },
    { label: "交付", used: pool("delivering").used, limit: pool("delivering").limit },
    { label: "录屏", used: pool("recording").used, limit: pool("recording").limit },
  ];
  return (
    <section className="dispatch-capacity" aria-label="资源占用">
      {cells.map((cell) => {
        const tone = cell.used > cell.limit ? "over" : cell.limit > 0 && cell.used >= cell.limit ? "full" : "free";
        return (
          <div key={cell.label} className="dispatch-capacity-cell" data-tone={tone} data-major={cell.major || undefined}>
            <span>{cell.label}</span>
            <strong className="mono">
              {cell.used}<small>/{cell.limit}</small>
            </strong>
            <div className="dispatch-capacity-bar" aria-hidden="true">
              <i style={{ width: `${cell.limit ? Math.min(100, (cell.used / cell.limit) * 100) : 0}%` }} />
            </div>
            <em>{tone === "over" ? "超出上限，收敛中" : tone === "full" ? "已满" : cell.note ?? `空闲 ${Math.max(0, cell.limit - cell.used)}`}</em>
          </div>
        );
      })}
    </section>
  );
}

function QueueRow({
  item,
  group,
  eta,
  onExplain,
  onAction,
  busy,
  readonly,
}: {
  item: DispatchQueueItem;
  group: Group;
  eta?: string;
  onExplain: () => void;
  onAction: (name: RowAction) => void;
  busy: boolean;
  readonly: boolean;
}) {
  const needs = group === "needs_action";
  const waiting = group === "waiting";
  const movable = needs || waiting;
  const until = item.wait_until || item.not_before;
  return (
    <article className="dispatch-row" data-status={item.status} data-group={group} data-keep={item.keep || undefined}>
      <div className="dispatch-row-status">
        <StatusDot
          state={item.active ? "running" : needs ? "blocked" : item.status === "done" ? "exported" : group === "finished" ? "idle" : "queued"}
          label={STAGE_LABEL[item.stage] ?? item.stage}
          pulse={item.active}
        />
        {item.queue_position && !item.active ? <span className="mono">#{item.queue_position}</span> : null}
      </div>
      <div className="dispatch-row-main">
        <Link className="dispatch-row-name" to={`/tasks/${item.task_id}`} title={item.task_name}>{item.task_name}</Link>
        {item.active ? (
          <span className="dispatch-row-sub">
            执行中 · 最近更新 <Elapsed since={item.updated_at} /> 前
          </span>
        ) : group === "finished" ? (
          <span className="dispatch-row-sub">{item.status === "done" ? "已完成" : item.status} · {formatDateTime(item.updated_at)}</span>
        ) : (
          <button type="button" className="dispatch-reason" onClick={onExplain} title="查看门禁明细">
            {item.reason}
          </button>
        )}
      </div>
      <div className="dispatch-row-meta mono">
        {waiting && until ? (
          <span>退避至 <Countdown at={until} /></span>
        ) : waiting && eta ? (
          <span>预计 <Countdown at={eta} /></span>
        ) : null}
        {item.total_attempts > 1 ? <span title="累计尝试次数">第 {item.total_attempts} 次</span> : null}
        {item.keep ? <Badge tone="info">保留</Badge> : null}
      </div>
      <div className="dispatch-row-actions">
        {needs || item.status === "backoff" ? (
          <Button size="small" variant={needs ? "primary" : "secondary"} loading={busy} disabled={readonly} onClick={() => onAction("retry")}>
            <ListRestart size={12} /> 重试
          </Button>
        ) : null}
        {movable ? (
          <>
            <Button size="small" variant="ghost" disabled={readonly || busy} onClick={() => onAction("pin")} title="置顶"><Pin size={12} /></Button>
            <Button size="small" variant="ghost" disabled={readonly || busy} onClick={() => onAction("up")} title="上移"><ArrowUp size={12} /></Button>
            <Button size="small" variant="ghost" disabled={readonly || busy} onClick={() => onAction("down")} title="下移"><ArrowDown size={12} /></Button>
          </>
        ) : null}
        {group !== "finished" ? (
          <details className="dispatch-more">
            <summary title="更多操作" aria-label="更多操作"><MoreHorizontal size={14} /></summary>
            <div>
              <button type="button" disabled={readonly || busy} onClick={() => onAction("keep")}>
                {item.keep ? "解除保留" : "保留"}
                <small>{item.keep ? "允许清理该题" : "结束后不自动清理"}</small>
              </button>
              {!item.active && !needs ? (
                <button type="button" disabled={readonly || busy} onClick={() => onAction("quarantine")}>
                  隔离<small>移出调度，等待人工处理</small>
                </button>
              ) : null}
              {!item.active ? (
                <button type="button" onClick={onExplain}>
                  门禁明细<small>逐条查看为何未启动</small>
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
  const elasticOn = Boolean(draft.elastic?.enabled);
  return (
    <fieldset className="dispatch-limit-form panel-body" disabled={readonly}>
      <LimitNumber label="容器数" hint="写回技能 maxContainers" value={draft.containers ?? snapshot.limits.containers} min={1} max={8} onChange={(containers) => setDraft({ ...draft, containers })} />
      <label className="dispatch-limit-row">
        <span>弹性扩容</span>
        <span className="dispatch-limit-inline">
          <input
            type="checkbox"
            checked={elasticOn}
            onChange={(event) => setDraft({ ...draft, elastic: { ...draft.elastic, enabled: event.target.checked } })}
          />
          <input
            className="compact-number"
            type="number"
            min={draft.containers ?? 1}
            max={8}
            disabled={!elasticOn}
            value={draft.elastic?.ceiling ?? snapshot.elastic.ceiling}
            onChange={(event) => setDraft({ ...draft, elastic: { ...draft.elastic, ceiling: Number(event.target.value) } })}
            aria-label="弹性上限"
          />
        </span>
        <small>{elasticOn ? `当前 ${snapshot.elastic.effective}` : "关闭"}</small>
      </label>
      <AutoLimit label="会话并发" value={draft.sessions ?? 0} autoValue={snapshot.limits.sessions} onChange={(sessions) => setDraft({ ...draft, sessions })} />
      <AutoLimit label="在手上限" value={draft.in_hand ?? 0} autoValue={snapshot.limits.in_hand} onChange={(in_hand) => setDraft({ ...draft, in_hand })} />
      <LimitNumber label="启动间隔" suffix="秒" value={draft.launch_spacing_seconds ?? 45} min={10} max={3600} onChange={(launch_spacing_seconds) => setDraft({ ...draft, launch_spacing_seconds })} />
      <LimitNumber label="录屏并发" value={draft.recording ?? 1} min={1} max={16} onChange={(recording) => setDraft({ ...draft, recording })} />
      <p className="dispatch-limit-note">会话、在手填 0 表示随预设自动计算。应用后预设切换为「自定义」。</p>
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

function LimitNumber({
  label,
  value,
  min,
  max,
  suffix,
  hint,
  onChange,
}: {
  label: string;
  value: number;
  min: number;
  max: number;
  suffix?: string;
  hint?: string;
  onChange: (value: number) => void;
}) {
  return (
    <label className="dispatch-limit-row" title={hint}>
      <span>{label}</span>
      <input className="compact-number" type="number" min={min} max={max} value={value} onChange={(event) => onChange(Number(event.target.value))} />
      <small>{suffix ?? ""}</small>
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

function groupQueue(queue: DispatchQueueItem[]): Record<Group, DispatchQueueItem[]> {
  const groups: Record<Group, DispatchQueueItem[]> = { needs_action: [], active: [], waiting: [], finished: [] };
  for (const item of queue) {
    if (item.status === "quarantined") groups.needs_action.push(item);
    else if (item.active) groups.active.push(item);
    else if (FINISHED.has(item.status)) groups.finished.push(item);
    else groups.waiting.push(item);
  }
  groups.waiting.sort((a, b) => (a.queue_position ?? 1e9) - (b.queue_position ?? 1e9));
  return groups;
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
