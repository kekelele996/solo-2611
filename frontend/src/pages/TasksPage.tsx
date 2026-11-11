import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Archive, Bot, Columns3, ListFilter, Play, Search, ShieldCheck, Trash2, Upload } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";

import { api } from "../api/client";
import { AutopilotPanel } from "../components/AutopilotPanel";
import { PurgeModal } from "../components/PurgeModal";
import type { TaskRow } from "../api/types";
import {
  Badge,
  Button,
  ConfirmModal,
  EmptyState,
  ErrorPanel,
  LoadingLines,
  Panel,
  StatusDot,
} from "../components/ui";
import { useToast } from "../components/toast-context";
import { useUIStore } from "../stores/ui";
import { errorText, STATUS_FILTERS, statusLabel } from "../utils/display";
import { formatDateTime } from "../utils/time";

/** 一次批量操作里每一行的结局：成功的行闪一下，失败的行留红条和原文。 */
type BatchOutcome = { done: string[]; failed: Map<string, string> };
type SavedView = { name: string; status: string; q: string; view: string };

function readSavedViews(): SavedView[] {
  try {
    const data = JSON.parse(window.localStorage.getItem("solo-auto-task-views") ?? "[]") as unknown;
    return Array.isArray(data) ? data.filter(isSavedView) : [];
  } catch {
    return [];
  }
}

function isSavedView(item: unknown): item is SavedView {
  if (!item || typeof item !== "object") return false;
  const value = item as Record<string, unknown>;
  return typeof value.name === "string"
    && typeof value.status === "string"
    && typeof value.q === "string"
    && typeof value.view === "string";
}

export function TasksPage() {
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const density = useUIStore((state) => state.density);
  const setDensity = useUIStore((state) => state.setDensity);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [cursor, setCursor] = useState(() => Number(params.get("sel") ?? 0) || 0);
  const [flash, setFlash] = useState<Set<string>>(new Set());
  const [failed, setFailed] = useState<Map<string, string>>(new Map());
  const [archiveOpen, setArchiveOpen] = useState(false);
  const [autoOpen, setAutoOpen] = useState(false);
  const [purgeOpen, setPurgeOpen] = useState(false);
  const [savedViews, setSavedViews] = useState<SavedView[]>(readSavedViews);
  const searchRef = useRef<HTMLInputElement>(null);
  const tableRef = useRef<HTMLTableElement>(null);
  const status = params.get("status") ?? "";
  const q = params.get("q") ?? "";
  const view = params.get("view") ?? "";
  const tasks = useQuery({
    queryKey: ["tasks", status, q],
    queryFn: () => api.tasks({ status, q, page_size: 200 }),
    refetchInterval: 3000,
  });
  const audits = useQuery({ queryKey: ["audits", "pending"], queryFn: () => api.audits("pending"), refetchInterval: 5000 });
  const pilot = useQuery({ queryKey: ["autopilot"], queryFn: api.autopilot, refetchInterval: 5000 });
  const auditIds = useMemo(() => new Set((audits.data?.items ?? []).map((item) => item.task_id)), [audits.data?.items]);
  const failedIds = useMemo(
    () => new Set((pilot.data?.jobs ?? []).filter((job) => job.stage === "failed").map((job) => job.task_id)),
    [pilot.data?.jobs],
  );
  const rows = useMemo(() => {
    const items = tasks.data?.items ?? [];
    if (view === "audit") return items.filter((task) => auditIds.has(task.id));
    if (view === "missing") return items.filter((task) => task.presence === "missing");
    if (view === "legacy") return items.filter((task) => isLegacy(task));
    if (view === "failed") return items.filter((task) => failedIds.has(task.id) || task.job_status === "blocked");
    return items;
  }, [auditIds, failedIds, tasks.data?.items, view]);
  const legacyRows = useMemo(() => rows.filter((task) => isLegacy(task)), [rows]);
  const mainRows = useMemo(() => (view ? rows : rows.filter((task) => !isLegacy(task))), [rows, view]);
  const refetch = tasks.refetch;

  useEffect(() => {
    if (params.get("sel") === String(cursor)) return;
    const next = new URLSearchParams(params);
    next.set("sel", String(cursor));
    setParams(next, { replace: true });
  }, [cursor, params, setParams]);

  useEffect(() => {
    setCursor((current) => Math.min(current, Math.max(0, rows.length - 1)));
  }, [rows.length]);

  useEffect(() => {
    if (!flash.size) return;
    const timer = window.setTimeout(() => setFlash(new Set()), 600);
    return () => window.clearTimeout(timer);
  }, [flash]);

  const toggle = (id: string) =>
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  useEffect(() => {
    const move = (delta: number) => {
      setCursor((index) => {
        const next = Math.min(rows.length - 1, Math.max(0, index + delta));
        tableRef.current?.querySelectorAll("tbody tr")[next]?.scrollIntoView?.({ block: "nearest" });
        return next;
      });
    };
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      if (target?.closest("input, textarea, select, [contenteditable]")) return;
      // 弹窗或命令面板开着的时候，列表不抢键
      if (document.querySelector(".modal-layer, .command-layer")) return;
      const command = event.metaKey || event.ctrlKey;
      if (command && event.key.toLowerCase() === "a") {
        event.preventDefault();
        setSelected(new Set(rows.map((task) => task.id)));
        return;
      }
      // 其余都是单键；带修饰键的组合（⌘F、⌘R…）留给浏览器
      if (command || event.altKey) return;
      const onControl = Boolean(target?.closest("button, a"));
      const task = rows[cursor];
      if (event.key === "j") move(1);
      if (event.key === "k") move(-1);
      if (event.key === "Enter" && task && !onControl) void navigate(`/tasks/${task.id}`);
      if (event.key === " " && task && !onControl) {
        event.preventDefault();
        toggle(task.id);
      }
      if (event.key === "x" && task) {
        toggle(task.id);
        move(1);
      }
      if (event.key === "Escape") setSelected(new Set());
      if (event.key === "f") {
        const next = new URLSearchParams(params);
        if (status === "blocked") next.delete("status");
        else next.set("status", "blocked");
        setParams(next);
      }
      if (event.key === "r") void refetch();
      if (event.key === "/") {
        event.preventDefault();
        searchRef.current?.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [cursor, navigate, params, refetch, rows, setParams, status]);

  const settle = (outcome: BatchOutcome) => {
    setFlash(new Set(outcome.done));
    setFailed(outcome.failed);
    setSelected(new Set(outcome.failed.keys()));
    void queryClient.invalidateQueries();
  };
  const runEach = async (ids: string[], action: (id: string) => Promise<unknown>): Promise<BatchOutcome> => {
    const results = await Promise.allSettled(ids.map((id) => action(id)));
    const outcome: BatchOutcome = { done: [], failed: new Map() };
    results.forEach((result, index) => {
      if (result.status === "fulfilled") outcome.done.push(ids[index]);
      else outcome.failed.set(ids[index], errorText(result.reason));
    });
    return outcome;
  };

  const batchQueue = useMutation({
    mutationFn: async () => {
      const chosen = rows.filter((task) => selected.has(task.id));
      const outcome = await runEach(
        chosen.filter((task) => task.job_status === "ready").map((task) => task.id),
        (id) => api.enqueue(id),
      );
      for (const task of chosen) {
        if (task.job_status !== "ready") outcome.failed.set(task.id, `当前状态是「${statusLabel(task.job_status)}」，不能排队`);
      }
      return outcome;
    },
    onSuccess: (outcome) => {
      toast(
        `已排队 ${outcome.done.length} 题${outcome.failed.size ? `，${outcome.failed.size} 题未排队（原因见各行）` : ""}`,
        outcome.failed.size ? "warn" : "ok",
      );
      settle(outcome);
    },
    onError: (error) => toast(errorText(error), "error"),
  });

  const batchExport = useMutation({
    mutationFn: () => api.export({ task_ids: [...selected] }),
    onSuccess: (result) => {
      const skipped = new Map(result.skipped.map((item) => [item.task_id, item.reason.join("；")]));
      if (!result.rows) {
        toast(`没有任务通过导出检查，${result.skipped.length} 题被跳过（原因见各行）`, "error");
        settle({ done: [], failed: skipped });
        return;
      }
      toast(`已导出 ${result.rows} 行，跳过 ${result.skipped.length} 题`, result.skipped.length ? "warn" : "ok");
      settle({ done: [...selected].filter((id) => !skipped.has(id)), failed: skipped });
      if (!result.skipped.length) void navigate("/export");
    },
    onError: (error) => toast(errorText(error), "error"),
  });
  const batchGates = useMutation({
    mutationFn: async () => {
      const outcome: BatchOutcome = { done: [], failed: new Map() };
      const results = await Promise.allSettled([...selected].map((taskId) => api.task(taskId)));
      [...selected].forEach((id, index) => {
        const result = results[index];
        if (result.status === "rejected") outcome.failed.set(id, errorText(result.reason));
        else if (result.value.complete) outcome.done.push(id);
        else outcome.failed.set(id, result.value.missing.join("；") || "门禁未通过");
      });
      return outcome;
    },
    onSuccess: (outcome) => {
      toast(
        `门禁通过 ${outcome.done.length} 题，未通过 ${outcome.failed.size} 题${outcome.failed.size ? "（缺项见各行）" : ""}`,
        outcome.failed.size ? "warn" : "ok",
      );
      settle(outcome);
    },
    onError: (error) => toast(errorText(error), "error"),
  });
  const batchArchive = useMutation({
    mutationFn: () => runEach([...selected], (id) => api.archiveTask(id)),
    onSuccess: (outcome) => {
      toast(
        `已归档 ${outcome.done.length} 题${outcome.failed.size ? `，失败 ${outcome.failed.size} 题（原因见各行）` : ""}`,
        outcome.failed.size ? "warn" : "ok",
      );
      setArchiveOpen(false);
      settle(outcome);
    },
    onError: (error) => {
      setArchiveOpen(false);
      toast(errorText(error), "error");
    },
  });

  const batchAuto = useMutation({
    mutationFn: () => api.autopilotStart([...selected]),
    onSuccess: (result) => {
      const skipped = selected.size - result.added.length;
      toast(`${result.added.length} 题进入自动交付${skipped ? `，${skipped} 题已在进行中` : ""}`, "ok");
      setAutoOpen(false);
      setSelected(new Set());
      void queryClient.invalidateQueries();
    },
    onError: (error) => {
      setAutoOpen(false);
      toast(errorText(error), "error");
    },
  });

  const updateFilter = (key: string, value: string) => {
    const next = new URLSearchParams(params);
    if (value) next.set(key, value);
    else next.delete(key);
    setParams(next, { replace: key === "q" });
  };
  const updateView = (nextView: string) => {
    const next = new URLSearchParams(params);
    if (nextView) next.set("view", nextView);
    else next.delete("view");
    setParams(next);
  };
  const saveView = () => {
    const name = window.prompt("视图名称");
    if (!name?.trim()) return;
    setSavedViews((current) => {
      const next = [...current.filter((item) => item.name !== name.trim()), { name: name.trim(), status, q, view }];
      window.localStorage.setItem("solo-auto-task-views", JSON.stringify(next));
      return next;
    });
  };
  const applyView = (item: SavedView) => {
    const next = new URLSearchParams();
    if (item.status) next.set("status", item.status);
    if (item.q) next.set("q", item.q);
    if (item.view) next.set("view", item.view);
    setParams(next);
  };
  const readySelected = rows.filter((task) => selected.has(task.id) && task.job_status === "ready").length;

  return (
    <div className={`page tasks-page density-${density}`}>
      <header className="page-head">
        <div>
          <span className="eyebrow">TASKS / LOCAL FACT LAYER</span>
          <h1>任务</h1>
        </div>
        <div className="page-actions">
          <Button
            size="small"
            variant="ghost"
            onClick={() => setDensity(density === "dense" ? "default" : "dense")}
            title="切换行高"
          >
            <Columns3 size={14} />
            {density === "dense" ? "默认行高" : "紧凑行高"}
          </Button>
        </div>
      </header>

      <div className="table-toolbar">
        <label className="search-box">
          <Search size={14} />
          <input
            ref={searchRef}
            value={q}
            onChange={(event) => updateFilter("q", event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Escape") event.currentTarget.blur();
            }}
            placeholder="搜索任务名、路径或 SessionID"
          />
          <kbd>/</kbd>
        </label>
        <label className="select-inline">
          <ListFilter size={14} />
          <select value={status} onChange={(event) => updateFilter("status", event.target.value)} aria-label="按状态筛选">
            {STATUS_FILTERS.map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </label>
        <span className="toolbar-count">{tasks.data?.total ?? 0} 个任务</span>
      </div>
      <div className="saved-views" aria-label="保存的筛选视图">
        <button type="button" data-active={!view || undefined} onClick={() => updateView("")}>全部</button>
        <button type="button" data-active={view === "audit" || undefined} onClick={() => updateView("audit")}>待抽检</button>
        <button type="button" data-active={view === "failed" || undefined} onClick={() => updateView("failed")}>失败</button>
        <button type="button" data-active={view === "legacy" || undefined} onClick={() => updateView("legacy")}>旧版遗留</button>
        <button type="button" data-active={view === "missing" || undefined} onClick={() => updateView("missing")}>目录缺失</button>
        {savedViews.map((item) => (
          <button type="button" key={item.name} onClick={() => applyView(item)}>{item.name}</button>
        ))}
        <button type="button" className="saved-view-add" onClick={saveView}>＋ 保存当前视图</button>
      </div>

      {selected.size ? (
        <div className="batch-bar">
          <strong>已选 {selected.size} 题</strong>
          <Button size="small" variant="primary" onClick={() => setAutoOpen(true)}>
            <Bot size={13} /> 自动交付
          </Button>
          <Button
            size="small"
            onClick={() => batchQueue.mutate()}
            loading={batchQueue.isPending}
            disabled={!readySelected}
            title={readySelected ? undefined : "所选任务里没有「可运行」状态的"}
          >
            <Play size={13} /> 批量排队{readySelected ? ` · ${readySelected}` : ""}
          </Button>
          <Button size="small" onClick={() => batchExport.mutate()} loading={batchExport.isPending}>
            <Upload size={13} /> 批量导出
          </Button>
          <Button size="small" onClick={() => batchGates.mutate()} loading={batchGates.isPending}>
            <ShieldCheck size={13} /> 检查门禁
          </Button>
          <Button size="small" variant="danger" onClick={() => setArchiveOpen(true)}>
            <Archive size={13} /> 归档归还
          </Button>
          <Button size="small" variant="danger" onClick={() => setPurgeOpen(true)}>
            <Trash2 size={13} /> 清理任务
          </Button>
          <Button size="small" variant="ghost" onClick={() => setSelected(new Set())}>
            取消选择
          </Button>
        </div>
      ) : null}

      <AutopilotPanel />

      {tasks.isError ? <ErrorPanel error={tasks.error} onRetry={() => { void tasks.refetch(); }} /> : null}
      {tasks.isPending ? <LoadingLines rows={8} /> : null}
      {tasks.data && !rows.length ? (
        <EmptyState
          title={status || q ? "没有符合当前筛选的任务。" : "还没有任务。先去题源领取一个工程。"}
          action={
            status || q ? (
              <Button onClick={() => setParams({})}>清除筛选</Button>
            ) : (
              <Button onClick={() => { void navigate("/source"); }}>去题源领取</Button>
            )
          }
        />
      ) : null}
      {rows.length ? (
        <Panel className="table-panel">
          <table className="data-table task-table" ref={tableRef}>
            <thead>
              <tr>
                <th className="check-cell">
                  <input
                    type="checkbox"
                    aria-label="全选当前列表"
                    checked={selected.size > 0 && rows.every((task) => selected.has(task.id))}
                    onChange={(event) => setSelected(event.target.checked ? new Set(rows.map((task) => task.id)) : new Set())}
                  />
                </th>
                <th>任务</th>
                <th>状态</th>
                <th>阻塞原因 / 下一步</th>
                <th className="numeric">进度</th>
                <th>SessionID</th>
                <th>创建时间</th>
              </tr>
            </thead>
            <tbody>
              {mainRows.map((task, index) => (
                <TaskTableRow
                  key={task.id}
                  task={task}
                  active={index === cursor}
                  selected={selected.has(task.id)}
                  flash={flash.has(task.id)}
                  failure={failed.get(task.id)}
                  auditPending={auditIds.has(task.id)}
                  onSelect={() => toggle(task.id)}
                  onOpen={() => {
                    setCursor(index);
                    void navigate(`/tasks/${task.id}`);
                  }}
                />
              ))}
            </tbody>
          </table>
        </Panel>
      ) : null}
      {!view && legacyRows.length ? (
        <details className="legacy-group">
          <summary>旧版遗留 · {legacyRows.length} <span>技能版本低于 0.2.3，默认折叠</span></summary>
          <div className="legacy-actions">
            <Button size="small" variant="danger" onClick={() => {
              setSelected(new Set(legacyRows.map((task) => task.id)));
              setPurgeOpen(true);
            }}>选择全部并清理</Button>
          </div>
          <div className="legacy-list">
            {legacyRows.map((task) => (
              <div key={task.id}>
                <button type="button" onClick={() => void navigate(`/tasks/${task.id}`)}>{task.task_name}</button>
                <span className="mono">skill {task.skill_version || "未知"}</span>
                <Badge tone="warn">旧版</Badge>
              </div>
            ))}
          </div>
        </details>
      ) : null}

      <ConfirmModal
        open={autoOpen}
        title={`自动交付 ${selected.size} 题`}
        confirmLabel="开始自动交付"
        loading={batchAuto.isPending}
        onConfirm={() => batchAuto.mutate()}
        onClose={() => setAutoOpen(false)}
      >
        <p>平台会从每题当前的进度接着走：拉起 Codex 出题 → 容器跑单轮 → Codex 复核、录屏、写 Feedback → 自动确认并导出。</p>
        <p>全程没有人工复核，Feedback 和分数以 Agent 写下的为准；确认记录会标注为「自动交付」。</p>
        <p>Agent 在本机以完整权限运行，录屏时会临时打开一个终端窗口。</p>
      </ConfirmModal>

      <PurgeModal
        taskIds={[...selected]}
        open={purgeOpen}
        onClose={() => setPurgeOpen(false)}
        onDone={(removed) => {
          setPurgeOpen(false);
          setSelected((current) => new Set([...current].filter((id) => !removed.includes(id))));
        }}
      />

      <ConfirmModal
        open={archiveOpen}
        title={`归档并归还 ${selected.size} 题`}
        confirmLabel="归档并归还"
        tone="danger"
        loading={batchArchive.isPending}
        onConfirm={() => batchArchive.mutate()}
        onClose={() => setArchiveOpen(false)}
      >
        <p>会先清理这些任务的容器，再把工程归还给工程台。</p>
        <p>归档后不可撤销；本地任务目录不会被删除。</p>
      </ConfirmModal>
    </div>
  );
}

function TaskTableRow({
  task,
  active,
  selected,
  flash,
  failure,
  auditPending,
  onSelect,
  onOpen,
}: {
  task: TaskRow;
  active: boolean;
  selected: boolean;
  flash: boolean;
  failure?: string;
  auditPending: boolean;
  onSelect: () => void;
  onOpen: () => void;
}) {
  const done = task.steps?.filter((step) => step.state === "done").length ?? 0;
  const note = failure ?? task.notes ?? nextAction(task);
  const className = [active ? "is-cursor" : "", flash ? "is-flash" : "", failure ? "is-failed" : ""].filter(Boolean).join(" ");
  return (
    <tr className={className} onClick={onOpen} aria-selected={selected}>
      <td className="check-cell" onClick={(event) => event.stopPropagation()}>
        <input type="checkbox" checked={selected} onChange={onSelect} aria-label={`选择 ${task.task_name}`} />
      </td>
      <td>
        <strong className="ellipsis" title={task.task_name}>{task.task_name}</strong>
        <span className="task-inline-badges">
          {task.ack_by === "autopilot" ? <Badge tone="info">自动</Badge> : null}
          {auditPending ? <Badge tone="warn">待抽检</Badge> : null}
          {isLegacy(task) ? <Badge tone="warn">旧版</Badge> : null}
          {task.presence === "missing" ? <Badge tone="danger">目录缺失</Badge> : null}
        </span>
        <small className="mono">{task.domain} · {task.task_type ?? "待定题"}</small>
      </td>
      <td><StatusDot state={task.job_status} label={statusLabel(task.job_status)} /></td>
      <td className={`ellipsis ${failure ? "danger-text" : ""}`} title={note}>{note}</td>
      <td className="numeric mono">{done}/11</td>
      <td className="mono ellipsis" title={task.session_id ?? undefined}>
        {task.session_id ? task.session_id.slice(0, 8) : "—"}
      </td>
      <td className="nowrap">{formatDateTime(task.created_at)}</td>
    </tr>
  );
}

function nextAction(task: TaskRow): string {
  const mapping: Record<string, string> = {
    blocked: "查看原始错误",
    running: "正在等待模型运行",
    queued: "等待容器名额",
    authored: "继续题面与论证",
    prompt_ok: "推送初始快照",
    ready: "可以开始运行",
    ran: "等待窗口录屏",
    recorded: "编写并检查 Feedback",
    reviewed: "生成 19 列 Excel",
    exported: "已进入交付",
    archived: "已归档",
    aborted: "已放弃",
    orphan: "state.json 缺失，查看详情",
  };
  return mapping[task.job_status] ?? "按左栏下一步处理";
}

function isLegacy(task: TaskRow): boolean {
  if (!task.skill_version) return false;
  const parts = task.skill_version.split(".").map((part) => Number.parseInt(part, 10) || 0);
  const value = (parts[0] ?? 0) * 10_000 + (parts[1] ?? 0) * 100 + (parts[2] ?? 0);
  return value < 203;
}
