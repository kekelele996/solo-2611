import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ClipboardCheck,
  AlertTriangle,
  Command,
  Gauge,
  Atom,
  FileSpreadsheet,
  FolderSearch,
  LayoutDashboard,
  ListChecks,
  Moon,
  Settings,
  ScrollText,
  Sun,
} from "lucide-react";
import { NavLink, Outlet } from "react-router-dom";

import { api } from "../api/client";
import { useEvents } from "../hooks/useEvents";
import { useHotkeys } from "../hooks/useHotkeys";
import { useUIStore } from "../stores/ui";
import { CommandPalette } from "./CommandPalette";
import { Button, EmptyState, Modal, StatusDot } from "./ui";
import { useToast } from "./toast-context";
import { APP_BUILD_ID } from "../utils/build";

const navItems = [
  { to: "/", label: "看板", icon: LayoutDashboard },
  { to: "/dispatch", label: "调度", icon: Gauge },
  { to: "/logs", label: "调度日志", icon: ScrollText },
  { to: "/tasks", label: "任务", icon: ListChecks },
  { to: "/audit", label: "抽检", icon: ClipboardCheck },
  { to: "/source", label: "题源", icon: FolderSearch },
  { to: "/export", label: "导出", icon: FileSpreadsheet },
  { to: "/system", label: "设置", icon: Settings },
];

export function AppShell() {
  useEvents();
  useHotkeys();
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const theme = useUIStore((state) => state.theme);
  const setTheme = useUIStore((state) => state.setTheme);
  const commandOpen = useUIStore((state) => state.commandOpen);
  const helpOpen = useUIStore((state) => state.helpOpen);
  const setCommandOpen = useUIStore((state) => state.setCommandOpen);
  const setHelpOpen = useUIStore((state) => state.setHelpOpen);
  const status = useQuery({
    queryKey: ["system-status"],
    queryFn: api.systemStatus,
    refetchInterval: 3000,
  });
  const version = useQuery({ queryKey: ["version"], queryFn: api.version, staleTime: 60_000 });
  const audits = useQuery({
    queryKey: ["audits", "pending"],
    queryFn: () => api.audits("pending"),
    refetchInterval: 5000,
  });
  // 与调度页共用缓存键，停留在调度页时不会重复拉取同一份快照。
  const dispatch = useQuery({
    queryKey: ["dispatch"],
    queryFn: api.dispatch,
    refetchInterval: 4000,
  });
  const toggleDispatch = useMutation({
    mutationFn: () => api.dispatchState(dispatch.data?.desired_state === "running" ? "paused" : "running"),
    onSuccess: () => {
      toast("调度状态已更新", "ok");
      void queryClient.invalidateQueries();
    },
    onError: (error) => toast(error instanceof Error ? error.message : "调度状态更新失败", "error"),
  });
  const versionMismatch = Boolean(version.data && version.data.build_id !== APP_BUILD_ID);

  return (
    <div className="app-shell">
      <header className="topbar">
        <button type="button" className="global-search" onClick={() => setCommandOpen(true)}>
          <Command size={15} />
          <span>搜索任务或命令</span>
          <kbd>⌘K</kbd>
        </button>
        <div className="topbar-counts">
          <button
            type="button"
            className="dispatch-topbar-badge"
            data-state={dispatch.data?.effective_state}
            onClick={() => toggleDispatch.mutate()}
            disabled={toggleDispatch.isPending || dispatch.data?.engine === "v2"}
            title={dispatch.data?.sentence || "打开调度"}
          >
            <StatusDot
              state={dispatch.data?.effective_state === "running" ? "running" : dispatch.data?.effective_state === "held" ? "blocked" : "idle"}
              label={dispatch.data?.effective_state === "running" ? "调度运行" : dispatch.data?.effective_state === "held" ? "调度挂起" : "调度暂停"}
              pulse={dispatch.data?.effective_state === "running"}
            />
            <span className="mono">
              {dispatch.data?.pools.containers.used ?? 0}/{dispatch.data?.pools.containers.limit ?? 0}
            </span>
          </button>
          <NavLink to="/tasks?status=running">
            <StatusDot
              state={status.data?.running ? "running" : "idle"}
              label={`运行 ${status.data?.running ?? 0}`}
              pulse={Boolean(status.data?.running)}
            />
          </NavLink>
          <NavLink to="/tasks?status=queued">
            <StatusDot state="queued" label={`排队 ${status.data?.queued ?? 0}`} />
          </NavLink>
          <NavLink to="/tasks?status=blocked">
            <StatusDot state={status.data?.blocked ? "blocked" : "idle"} label={`卡住 ${status.data?.blocked ?? 0}`} />
          </NavLink>
        </div>
        <div className="topbar-meta">
          <span className={status.data?.disk_low ? "warn-text" : ""}>
            磁盘 {status.data?.disk_free_gb ?? "—"}G
          </span>
          <span className="mono" title="solo-2611 技能版本">skill {version.data?.skill_version ?? "—"}</span>
          <button
            type="button"
            className="icon-button"
            title={theme === "dark" ? "切换浅色主题" : "切换深色主题"}
            aria-label={theme === "dark" ? "切换浅色主题" : "切换深色主题"}
            onClick={() => setTheme(theme === "dark" ? "light" : "dark")}
          >
            {theme === "dark" ? <Sun size={16} /> : <Moon size={16} />}
          </button>
        </div>
      </header>
      {versionMismatch ? (
        <div className="system-banner version-banner">
          <AlertTriangle size={15} />
          <span>
            页面与后端版本不一致，请刷新页面加载当前构建（页面 {APP_BUILD_ID} / 后端 {version.data?.build_id || "旧版"}）
          </span>
          <Button size="small" onClick={() => window.location.reload()}>
            刷新页面
          </Button>
        </div>
      ) : null}
      {status.data?.offline ||
      status.data?.docker_ok === false ||
      status.data?.disk_low ||
      Boolean(status.data?.auth_blocked) ? (
        <div className="system-banner">
          <AlertTriangle size={15} />
          <span>
            {status.data?.offline ? "工程台离线：历史任务仍可查看，新领取已禁用。" : ""}
            {status.data?.docker_ok === false ? " Docker 不可用：新运行会留在队列。" : ""}
            {status.data?.auth_blocked ? " 网关鉴权失败：运行队列已熔断，请检查本期团队 Key。" : ""}
            {status.data?.disk_low ? " 磁盘低于安全阈值，新领取和建题已禁用。" : ""}
          </span>
        </div>
      ) : null}
      <div className="app-body">
        <nav className="side-nav" aria-label="主导航">
          <div className="nav-mark" aria-label="solo-auto">
            <Atom size={20} />
          </div>
          {navItems.map((item) => {
            const Icon = item.icon;
            return (
              <NavLink key={item.to} to={item.to} end={item.to === "/"} title={item.label}>
                <Icon size={19} />
                <span>{item.label}</span>
                {item.to === "/audit" && audits.data?.total ? (
                  <i className="nav-badge">{audits.data.total}</i>
                ) : null}
              </NavLink>
            );
          })}
        </nav>
        <div className="page-frame">
          <Outlet />
        </div>
      </div>
      {commandOpen ? <CommandPalette /> : null}
      <Modal open={helpOpen} title="快捷键" onClose={() => setHelpOpen(false)}>
        <div className="shortcut-grid">
          <span>⌘K</span><strong>命令面板</strong>
          <span>g h / d / l / a / t / s / e / ,</span><strong>切换页面</strong>
          <span>j / k · Enter</span><strong>任务列表上下移动 · 打开</strong>
          <span>x / 空格 · ⌘A</span><strong>勾选当前行 · 全选</strong>
          <span>/ · f · r</span><strong>搜索 · 只看卡住 · 刷新</strong>
          <span>1–8</span><strong>任务详情 Tab</strong>
          <span>[ / ]</span><strong>上一题 / 下一题</strong>
          <span>⌘Enter</span><strong>执行当前主操作</strong>
          <span>⌘S</span><strong>保存当前编辑</strong>
          <span>⌘O · ⌘⇧C</span><strong>打开任务目录 · 复制关键证据</strong>
          <span>?</span><strong>打开本帮助</strong>
        </div>
      </Modal>
      <div className="viewport-blocker" role="alert">
        <strong>窗口太小</strong>
        <span>这个作业台需要至少 1024px 宽度。请放大窗口后再继续。</span>
      </div>
    </div>
  );
}

export function NotFoundPage() {
  return (
    <EmptyState
      icon={<AlertTriangle size={28} />}
      title="页面不存在"
      action={
        <Button onClick={() => window.location.assign("/")}>
          返回看板
        </Button>
      }
    />
  );
}
