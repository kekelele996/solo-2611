import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { LucideIcon } from "lucide-react";
import {
  ClipboardCheck,
  Command,
  Factory,
  FolderSearch,
  History,
  LayoutDashboard,
  ListChecks,
  Pause,
  Play,
  Power,
  RefreshCw,
  Settings,
  Trash2,
  Upload,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";

import { api } from "../api/client";
import { useUIStore } from "../stores/ui";
import { errorText, statusLabel } from "../utils/display";
import { todayShanghai } from "../utils/time";
import { useToast } from "./toast-context";

const pageCommands = [
  { label: "作业看板", to: "/", icon: LayoutDashboard },
  { label: "生产线", to: "/production", icon: Factory },
  { label: "人工抽检", to: "/audit", icon: ClipboardCheck },
  { label: "任务列表", to: "/tasks", icon: ListChecks },
  { label: "题源领取", to: "/source", icon: FolderSearch },
  { label: "导出与统计", to: "/export", icon: Upload },
  { label: "使用统计", to: "/usage", icon: History },
  { label: "回收站", to: "/system#trash", icon: Trash2 },
  { label: "系统设置", to: "/system", icon: Settings },
];

type Entry = {
  key: string;
  group: "命令" | "任务" | "页面";
  label: string;
  hint?: string;
  icon?: LucideIcon;
  disabled?: boolean;
  run: () => void;
};

export function CommandPalette() {
  const open = useUIStore((state) => state.commandOpen);
  const setOpen = useUIStore((state) => state.setCommandOpen);
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const results = useRef<HTMLDivElement>(null);
  const close = () => {
    setOpen(false);
    setQuery("");
  };
  const tasks = useQuery({
    queryKey: ["tasks", "command"],
    queryFn: () => api.tasks({ page_size: 200 }),
    enabled: open,
  });
  const readyCount = (tasks.data?.items ?? []).filter((task) => task.job_status === "ready").length;

  const bulkQueue = useMutation({
    mutationFn: async () => {
      const ready = (tasks.data?.items ?? []).filter((task) => task.job_status === "ready");
      const settled = await Promise.allSettled(ready.map((task) => api.enqueue(task.id)));
      const failed = settled.filter((result) => result.status === "rejected");
      return { done: settled.length - failed.length, firstError: failed[0]?.reason as unknown };
    },
    onSuccess: (result) => {
      if (result.firstError) toast(`已排队 ${result.done} 题；失败原因：${errorText(result.firstError)}`, "warn");
      else toast(`已排队 ${result.done} 个任务`, "ok");
      void queryClient.invalidateQueries();
      close();
    },
    onError: (error) => toast(errorText(error), "error"),
  });
  const exportToday = useMutation({
    mutationFn: () => api.export({ date: todayShanghai() }),
    onSuccess: (result) => {
      if (result.rows) toast(`已导出 ${result.rows} 行，跳过 ${result.skipped.length} 题`, result.skipped.length ? "warn" : "ok");
      else toast(`今天没有可导出的任务，跳过 ${result.skipped.length} 题`, "warn");
      void queryClient.invalidateQueries();
      close();
      void navigate("/export");
    },
    onError: (error) => toast(errorText(error), "error"),
  });
  const reconcile = useMutation({
    mutationFn: api.reconcile,
    onSuccess: (result) => {
      toast(`对账完成，修正 ${result.corrected} 项`, "ok");
      void queryClient.invalidateQueries();
      close();
    },
    onError: (error) => toast(errorText(error), "error"),
  });
  const pause = useMutation({
    mutationFn: api.autopilotPause,
    onSuccess: () => {
      toast("自动交付已暂停", "info");
      void queryClient.invalidateQueries();
      close();
    },
    onError: (error) => toast(errorText(error), "error"),
  });
  const restart = useMutation({
    mutationFn: () => api.restart(false),
    onSuccess: (result) => {
      toast(result.hint, "info");
      close();
    },
    onError: (error) => toast(errorText(error), "error"),
  });

  const entries = useMemo<Entry[]>(() => {
    // ">" 开头只搜命令；否则任务、页面、命令一起搜
    const commandOnly = query.startsWith(">");
    const term = query.replace(/^>/, "").trim().toLowerCase();
    const match = (text: string) => !term || text.toLowerCase().includes(term);
    const go = (to: string) => () => {
      void navigate(to);
      close();
    };
    const commands: Entry[] = [
      {
        key: "restart",
        group: "命令",
        label: "重启后端",
        icon: Power,
        disabled: restart.isPending,
        run: () => restart.mutate(),
      },
      {
        key: "pause",
        group: "命令",
        label: "暂停自动交付",
        icon: Pause,
        disabled: pause.isPending,
        run: () => pause.mutate(),
      },
      {
        key: "audit",
        group: "命令",
        label: "去抽检",
        icon: ClipboardCheck,
        run: go("/audit"),
      },
      {
        key: "production",
        group: "命令",
        label: "今天交付 N 题",
        icon: Factory,
        run: go("/production"),
      },
      {
        key: "trash",
        group: "命令",
        label: "打开回收站",
        icon: Trash2,
        run: go("/system#trash"),
      },
      {
        key: "queue",
        group: "命令",
        label: "排队全部已就绪任务",
        hint: readyCount ? `${readyCount} 题` : "暂无可运行任务",
        icon: Play,
        disabled: !readyCount || bulkQueue.isPending,
        run: () => bulkQueue.mutate(),
      },
      {
        key: "export",
        group: "命令",
        label: "导出今天",
        icon: Upload,
        disabled: exportToday.isPending,
        run: () => exportToday.mutate(),
      },
      {
        key: "reconcile",
        group: "命令",
        label: "手动对账磁盘索引",
        icon: RefreshCw,
        disabled: reconcile.isPending,
        run: () => reconcile.mutate(),
      },
    ];
    const taskEntries: Entry[] = commandOnly
      ? []
      : (tasks.data?.items ?? [])
          .filter((task) => match(task.task_name) || match(task.session_id ?? ""))
          .slice(0, 8)
          .map((task) => ({
            key: `task-${task.id}`,
            group: "任务",
            label: task.task_name,
            hint: statusLabel(task.job_status),
            run: go(`/tasks/${task.id}`),
          }));
    const pages: Entry[] = commandOnly
      ? []
      : pageCommands
          .filter((page) => match(page.label))
          .map((page) => ({ key: `page-${page.to}`, group: "页面", label: page.label, icon: page.icon, run: go(page.to) }));
    const visibleCommands = commands.filter((command) => match(command.label));
    // 有搜索词时任务排前面，空输入时命令排前面
    return term && !commandOnly ? [...taskEntries, ...pages, ...visibleCommands] : [...visibleCommands, ...taskEntries, ...pages];
    // eslint-disable-next-line react-hooks/exhaustive-deps -- close/navigate 每次渲染都是新的，但行为不变
  }, [
    query,
    tasks.data?.items,
    readyCount,
    bulkQueue.isPending,
    exportToday.isPending,
    reconcile.isPending,
    pause.isPending,
    restart.isPending,
  ]);

  useEffect(() => setActive(0), [query]);
  useEffect(() => {
    results.current?.querySelector('[aria-selected="true"]')?.scrollIntoView?.({ block: "nearest" });
  }, [active]);

  if (!open) return null;
  const current = Math.min(active, Math.max(0, entries.length - 1));
  const move = (delta: number) => {
    if (!entries.length) return;
    let next = current;
    for (let step = 0; step < entries.length; step += 1) {
      next = (next + delta + entries.length) % entries.length;
      if (!entries[next].disabled) break;
    }
    setActive(next);
  };
  const groups = entries.reduce<Array<{ name: string; items: Array<Entry & { index: number }> }>>((list, entry, index) => {
    const last = list[list.length - 1];
    if (last?.name === entry.group) last.items.push({ ...entry, index });
    else list.push({ name: entry.group, items: [{ ...entry, index }] });
    return list;
  }, []);

  return (
    <div className="command-layer" onMouseDown={close}>
      <section className="command-panel" role="dialog" aria-label="命令面板" onMouseDown={(event) => event.stopPropagation()}>
        <div className="command-input">
          <Command size={16} />
          <input
            autoFocus
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="搜索任务、页面；输入 > 只看命令"
            role="combobox"
            aria-expanded="true"
            aria-controls="command-results"
            onKeyDown={(event) => {
              if (event.nativeEvent.isComposing) return;
              if (event.key === "Escape") close();
              if (event.key === "ArrowDown") {
                event.preventDefault();
                move(1);
              }
              if (event.key === "ArrowUp") {
                event.preventDefault();
                move(-1);
              }
              if (event.key === "Enter") {
                const entry = entries[current];
                if (entry && !entry.disabled) entry.run();
              }
            }}
          />
          <kbd>Esc</kbd>
        </div>
        <div className="command-results" id="command-results" role="listbox" ref={results}>
          {groups.map((group) => (
            <div className="command-group" key={group.name}>
              <span>{group.name}</span>
              {group.items.map((entry) => {
                const Icon = entry.icon;
                return (
                  <button
                    type="button"
                    key={entry.key}
                    role="option"
                    aria-selected={entry.index === current}
                    disabled={entry.disabled}
                    onMouseMove={() => setActive(entry.index)}
                    onClick={entry.run}
                  >
                    {Icon ? <Icon size={15} /> : null}
                    <span>{entry.label}</span>
                    {entry.hint ? <small>{entry.hint}</small> : null}
                  </button>
                );
              })}
            </div>
          ))}
          {!entries.length ? <div className="command-empty">没有匹配项。</div> : null}
        </div>
        <div className="command-foot">
          <span><kbd>↑↓</kbd> 选择</span>
          <span><kbd>Enter</kbd> 执行</span>
          <span><kbd>&gt;</kbd> 只看命令</span>
        </div>
      </section>
    </div>
  );
}
