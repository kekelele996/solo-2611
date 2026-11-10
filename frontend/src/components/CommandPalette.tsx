import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Command, FolderSearch, LayoutDashboard, ListChecks, Play, RefreshCw, Settings, Upload } from "lucide-react";
import { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";

import { api } from "../api/client";
import { useUIStore } from "../stores/ui";
import { todayShanghai } from "../utils/time";
import { useToast } from "./toast-context";

const pageCommands = [
  { label: "作业看板", to: "/", icon: LayoutDashboard },
  { label: "任务列表", to: "/tasks", icon: ListChecks },
  { label: "题源领取", to: "/source", icon: FolderSearch },
  { label: "导出与统计", to: "/export", icon: Upload },
  { label: "系统设置", to: "/system", icon: Settings },
];

export function CommandPalette() {
  const open = useUIStore((state) => state.commandOpen);
  const setOpen = useUIStore((state) => state.setCommandOpen);
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const [query, setQuery] = useState("");
  const close = () => {
    setOpen(false);
    setQuery("");
  };
  const tasks = useQuery({
    queryKey: ["tasks", "command"],
    queryFn: () => api.tasks({ page_size: 200 }),
    enabled: open,
  });

  const filteredTasks = useMemo(() => {
    const term = query.trim().toLowerCase();
    const rows = tasks.data?.items ?? [];
    if (!term) return rows.slice(0, 8);
    return rows.filter((task) => task.task_name.toLowerCase().includes(term)).slice(0, 8);
  }, [query, tasks.data?.items]);
  const filteredPages = pageCommands.filter((item) => !query || item.label.includes(query));
  const bulkQueue = useMutation({
    mutationFn: async () => {
      const ready = (tasks.data?.items ?? []).filter((task) => task.job_status === "ready");
      const results = await Promise.allSettled(ready.map((task) => api.enqueue(task.id)));
      return results.filter((result) => result.status === "fulfilled").length;
    },
    onSuccess: (count) => {
      toast(`已排队 ${count} 个任务`, "ok");
      void queryClient.invalidateQueries();
      close();
    },
    onError: (error) => toast(error instanceof Error ? error.message : String(error), "error"),
  });
  const exportToday = useMutation({
    mutationFn: () => api.export({ date: todayShanghai() }),
    onSuccess: (result) => {
      toast(`已导出 ${result.rows} 行`, "ok");
      close();
    },
    onError: (error) => toast(error instanceof Error ? error.message : String(error), "error"),
  });
  const reconcile = useMutation({
    mutationFn: api.reconcile,
    onSuccess: (result) => {
      toast(`对账完成，修正 ${result.corrected} 项`, "ok");
      void queryClient.invalidateQueries();
      close();
    },
    onError: (error) => toast(error instanceof Error ? error.message : String(error), "error"),
  });

  if (!open) return null;
  return (
    <div className="command-layer" onMouseDown={close}>
      <section className="command-panel" onMouseDown={(event) => event.stopPropagation()}>
        <div className="command-input">
          <Command size={16} />
          <input
            autoFocus
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="搜索任务、页面或命令"
            onKeyDown={(event) => {
              if (event.key === "Escape") close();
              if (event.key === "Enter") {
                const task = filteredTasks[0];
                const page = filteredPages[0];
                if (task) {
                  void navigate(`/tasks/${task.id}`);
                  close();
                } else if (page) {
                  void navigate(page.to);
                  close();
                }
              }
            }}
          />
          <kbd>Esc</kbd>
        </div>
        <div className="command-results">
          {query.startsWith(">") || !query ? (
            <div className="command-group">
              <span>命令</span>
              <button onClick={() => bulkQueue.mutate()}>
                <Play size={15} />
                <span>排队全部已就绪任务</span>
              </button>
              <button onClick={() => exportToday.mutate()}>
                <Upload size={15} />
                <span>导出今天</span>
              </button>
              <button onClick={() => reconcile.mutate()}>
                <RefreshCw size={15} />
                <span>手动对账磁盘索引</span>
              </button>
            </div>
          ) : null}
          {filteredTasks.length ? (
            <div className="command-group">
              <span>任务</span>
              {filteredTasks.map((task) => (
                <button
                  key={task.id}
                  onClick={() => {
                    void navigate(`/tasks/${task.id}`);
                    close();
                  }}
                >
                  <span>{task.task_name}</span>
                  <small>{task.job_status}</small>
                </button>
              ))}
            </div>
          ) : null}
          {filteredPages.length ? (
            <div className="command-group">
              <span>页面</span>
              {filteredPages.map((page) => {
                const Icon = page.icon;
                return (
                  <button
                    key={page.to}
                    onClick={() => {
                      void navigate(page.to);
                      close();
                    }}
                  >
                    <Icon size={15} />
                    <span>{page.label}</span>
                  </button>
                );
              })}
            </div>
          ) : null}
          {!filteredTasks.length && !filteredPages.length ? (
            <div className="command-empty">没有匹配项。最近任务仍可从任务列表打开。</div>
          ) : null}
        </div>
      </section>
    </div>
  );
}
