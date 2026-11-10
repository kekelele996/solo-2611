import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Archive, Columns3, ListFilter, Play, Search, ShieldCheck, Upload } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";

import { api } from "../api/client";
import type { TaskRow } from "../api/types";
import {
  Button,
  EmptyState,
  ErrorPanel,
  LoadingLines,
  Panel,
  StatusDot,
} from "../components/ui";
import { useToast } from "../components/toast-context";
import { useUIStore } from "../stores/ui";
import { formatDateTime } from "../utils/time";

const statusOptions = [
  ["", "全部状态"],
  ["blocked", "卡住"],
  ["running", "运行中"],
  ["ready", "可运行"],
  ["ran", "待录屏"],
  ["recorded", "待反馈"],
  ["reviewed", "待导出"],
  ["exported", "已导出"],
];

export function TasksPage() {
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const density = useUIStore((state) => state.density);
  const setDensity = useUIStore((state) => state.setDensity);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [cursor, setCursor] = useState(() => Number(params.get("sel") ?? 0));
  const searchRef = useRef<HTMLInputElement>(null);
  const status = params.get("status") ?? "";
  const q = params.get("q") ?? "";
  const tasks = useQuery({
    queryKey: ["tasks", status, q],
    queryFn: () => api.tasks({ status, q, page_size: 200 }),
    refetchInterval: 3000,
  });
  const rows = useMemo(() => tasks.data?.items ?? [], [tasks.data?.items]);

  useEffect(() => {
    const next = new URLSearchParams(params);
    next.set("sel", String(cursor));
    setParams(next, { replace: true });
  }, [cursor, params, setParams]);

  useEffect(() => {
    setCursor((current) => Math.min(current, Math.max(0, rows.length - 1)));
  }, [rows.length]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      if (target?.tagName === "INPUT" || target?.tagName === "TEXTAREA") return;
      if (event.key === "j") setCursor((index) => Math.min(rows.length - 1, index + 1));
      if (event.key === "k") setCursor((index) => Math.max(0, index - 1));
      if (event.key === "Enter" && rows[cursor]) void navigate(`/tasks/${rows[cursor].id}`);
      if (event.key === " ") {
        event.preventDefault();
        const task = rows[cursor];
        if (task) {
          setSelected((current) => {
            const next = new Set(current);
            if (next.has(task.id)) next.delete(task.id);
            else next.add(task.id);
            return next;
          });
        }
      }
      if (event.key === "x") {
        const task = rows[cursor];
        if (task) {
          setSelected((current) => new Set(current).add(task.id));
          setCursor((index) => Math.min(rows.length - 1, index + 1));
        }
      }
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "a") {
        event.preventDefault();
        setSelected(new Set(rows.map((task) => task.id)));
      }
      if (event.key === "f") {
        event.preventDefault();
        const next = new URLSearchParams(params);
        if (status === "blocked") next.delete("status");
        else next.set("status", "blocked");
        setParams(next);
      }
      if (event.key === "r") void tasks.refetch();
      if (event.key === "/") {
        event.preventDefault();
        searchRef.current?.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [cursor, navigate, params, rows, setParams, status, tasks]);

  const batchQueue = useMutation({
    mutationFn: async () => {
      const targets = rows.filter((task) => selected.has(task.id) && task.job_status === "ready");
      const results = await Promise.allSettled(targets.map((task) => api.enqueue(task.id)));
      return {
        done: results.filter((result) => result.status === "fulfilled").length,
        skipped: selected.size - targets.length + results.filter((result) => result.status === "rejected").length,
      };
    },
    onSuccess: (result) => {
      toast(`已排队 ${result.done} 题，跳过 ${result.skipped} 题`, result.skipped ? "warn" : "ok");
      setSelected(new Set());
      void queryClient.invalidateQueries();
    },
    onError: (error) => toast(error instanceof Error ? error.message : String(error), "error"),
  });

  const batchExport = useMutation({
    mutationFn: () => api.export({ task_ids: [...selected] }),
    onSuccess: (result) => {
      toast(`已导出 ${result.rows} 行，跳过 ${result.skipped.length} 题`, result.skipped.length ? "warn" : "ok");
      setSelected(new Set());
      void navigate("/export");
    },
    onError: (error) => toast(error instanceof Error ? error.message : String(error), "error"),
  });
  const batchGates = useMutation({
    mutationFn: async () => {
      const results = await Promise.all([...selected].map((taskId) => api.task(taskId)));
      return {
        passed: results.filter((task) => task.complete).length,
        blocked: results.filter((task) => !task.complete).length,
      };
    },
    onSuccess: (result) => toast(`门禁通过 ${result.passed} 题，未通过 ${result.blocked} 题`, result.blocked ? "warn" : "ok"),
    onError: (error) => toast(error instanceof Error ? error.message : String(error), "error"),
  });
  const batchArchive = useMutation({
    mutationFn: async () => {
      const results = await Promise.allSettled([...selected].map((taskId) => api.archiveTask(taskId)));
      return {
        archived: results.filter((result) => result.status === "fulfilled").length,
        failed: results.filter((result) => result.status === "rejected").length,
      };
    },
    onSuccess: (result) => {
      toast(`已归档 ${result.archived} 题，失败 ${result.failed} 题`, result.failed ? "warn" : "ok");
      setSelected(new Set());
      void queryClient.invalidateQueries();
    },
    onError: (error) => toast(error instanceof Error ? error.message : String(error), "error"),
  });

  const updateFilter = (key: string, value: string) => {
    const next = new URLSearchParams(params);
    if (value) next.set(key, value);
    else next.delete(key);
    setParams(next);
  };

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
            title="切换密度"
          >
            <Columns3 size={14} />
            {density === "dense" ? "默认" : "紧凑"}
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
            placeholder="搜索任务名、路径或 SessionID"
          />
          <kbd>/</kbd>
        </label>
        <label className="select-inline">
          <ListFilter size={14} />
          <select value={status} onChange={(event) => updateFilter("status", event.target.value)}>
            {statusOptions.map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </label>
        <span className="toolbar-count">{tasks.data?.total ?? 0} 个任务</span>
      </div>

      {selected.size ? (
        <div className="batch-bar">
          <strong>已选 {selected.size} 题</strong>
          <Button size="small" onClick={() => batchQueue.mutate()} loading={batchQueue.isPending}>
            <Play size={13} /> 批量排队
          </Button>
          <Button size="small" onClick={() => batchExport.mutate()} loading={batchExport.isPending}>
            <Upload size={13} /> 批量导出
          </Button>
          <Button size="small" onClick={() => batchGates.mutate()} loading={batchGates.isPending}>
            <ShieldCheck size={13} /> 检查门禁
          </Button>
          <Button
            size="small"
            variant="danger"
            loading={batchArchive.isPending}
            onClick={() => {
              if (window.confirm("将先清理容器再归还工程，归档后不可撤销。确认继续？")) batchArchive.mutate();
            }}
          >
            <Archive size={13} /> 归档归还
          </Button>
          <Button size="small" variant="ghost" onClick={() => setSelected(new Set())}>
            取消选择
          </Button>
        </div>
      ) : null}

      {tasks.isError ? <ErrorPanel error={tasks.error} onRetry={() => { void tasks.refetch(); }} /> : null}
      {tasks.isPending ? <LoadingLines rows={8} /> : null}
      {tasks.data && !rows.length ? (
        <EmptyState title="没有符合当前筛选的任务。" action={<Button onClick={() => setParams({})}>清除筛选</Button>} />
      ) : null}
      {rows.length ? (
        <Panel className="table-panel">
          <table className="data-table task-table">
            <thead>
              <tr>
                <th className="check-cell"><span className="sr-only">选择</span></th>
                <th>任务</th>
                <th>状态</th>
                <th>阻塞原因 / 下一步</th>
                <th className="numeric">进度</th>
                <th>SessionID</th>
                <th>创建时间</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((task, index) => (
                <TaskTableRow
                  key={task.id}
                  task={task}
                  active={index === cursor}
                  selected={selected.has(task.id)}
                  onSelect={() => {
                    const next = new Set(selected);
                    if (next.has(task.id)) next.delete(task.id);
                    else next.add(task.id);
                    setSelected(next);
                  }}
                  onOpen={() => { void navigate(`/tasks/${task.id}`); }}
                  onFocus={() => setCursor(index)}
                />
              ))}
            </tbody>
          </table>
        </Panel>
      ) : null}
    </div>
  );
}

function TaskTableRow({
  task,
  active,
  selected,
  onSelect,
  onOpen,
  onFocus,
}: {
  task: TaskRow;
  active: boolean;
  selected: boolean;
  onSelect: () => void;
  onOpen: () => void;
  onFocus: () => void;
}) {
  const done = task.steps?.filter((step) => step.state === "done").length ?? 0;
  return (
    <tr className={active ? "is-cursor" : ""} onClick={onOpen} onMouseEnter={onFocus}>
      <td className="check-cell" onClick={(event) => event.stopPropagation()}>
        <input type="checkbox" checked={selected} onChange={onSelect} aria-label={`选择 ${task.task_name}`} />
      </td>
      <td>
        <strong>{task.task_name}</strong>
        <small className="mono">{task.domain} · {task.task_type ?? "待定题"}</small>
      </td>
      <td><StatusDot state={task.job_status} label={task.job_status} /></td>
      <td className="ellipsis">{task.notes || nextAction(task)}</td>
      <td className="numeric mono">{done}/11</td>
      <td className="mono ellipsis">{task.session_id ? `${task.session_id.slice(0, 8)}…` : "—"}</td>
      <td className="nowrap">{formatDateTime(task.created_at)}</td>
    </tr>
  );
}

function nextAction(task: TaskRow): string {
  const mapping: Record<string, string> = {
    blocked: "查看原始错误",
    running: "正在等待模型运行",
    authored: "继续题面与论证",
    prompt_ok: "推送初始快照",
    ready: "可以开始运行",
    ran: "等待窗口录屏",
    recorded: "编写并检查 Feedback",
    reviewed: "生成 19 列 Excel",
    exported: "已进入交付",
  };
  return mapping[task.job_status] ?? "按左栏下一步处理";
}
