import { useQuery } from "@tanstack/react-query";
import {
  AlertTriangle,
  Command,
  Atom,
  FileSpreadsheet,
  FolderSearch,
  LayoutDashboard,
  ListChecks,
  Moon,
  Settings,
  Sun,
} from "lucide-react";
import { useEffect } from "react";
import { NavLink, Outlet } from "react-router-dom";

import { api } from "../api/client";
import { useEvents } from "../hooks/useEvents";
import { useHotkeys } from "../hooks/useHotkeys";
import { useUIStore } from "../stores/ui";
import { CommandPalette } from "./CommandPalette";
import { Button, EmptyState, Modal, StatusDot } from "./ui";

const navItems = [
  { to: "/", label: "看板", icon: LayoutDashboard },
  { to: "/tasks", label: "任务", icon: ListChecks },
  { to: "/source", label: "题源", icon: FolderSearch },
  { to: "/export", label: "导出", icon: FileSpreadsheet },
  { to: "/system", label: "设置", icon: Settings },
];

export function AppShell() {
  useEvents();
  useHotkeys();
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

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
  }, [theme]);

  return (
    <div className="app-shell">
      <header className="topbar">
        <button className="global-search" onClick={() => setCommandOpen(true)}>
          <Command size={15} />
          <span>⌘K 搜索</span>
        </button>
        <div className="topbar-counts">
          <NavLink to="/tasks?status=running">
            <StatusDot state="running" label={`运行 ${status.data?.running ?? 0}`} />
          </NavLink>
          <NavLink to="/tasks?status=queued">
            <StatusDot state="queued" label={`排队 ${status.data?.queued ?? 0}`} />
          </NavLink>
          <NavLink to="/tasks?status=blocked">
            <StatusDot state="blocked" label={`卡住 ${status.data?.blocked ?? 0}`} />
          </NavLink>
        </div>
        <div className="topbar-meta">
          <span className={status.data?.disk_low ? "warn-text" : ""}>
            磁盘 {status.data?.disk_free_gb ?? "—"}G
          </span>
          <span className="mono">{version.data?.skill_version ?? "—"}</span>
          <button
            className="icon-button"
            aria-label={theme === "dark" ? "切换浅色主题" : "切换深色主题"}
            onClick={() => setTheme(theme === "dark" ? "light" : "dark")}
          >
            {theme === "dark" ? <Sun size={16} /> : <Moon size={16} />}
          </button>
        </div>
      </header>
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
          <span>g d / t / s / e / ,</span><strong>切换页面</strong>
          <span>j / k</span><strong>列表上下移动</strong>
          <span>1–7</span><strong>任务详情 Tab</strong>
          <span>[ / ]</span><strong>上一题 / 下一题</strong>
          <span>⌘Enter</span><strong>执行当前主操作</strong>
          <span>⌘S</span><strong>保存当前编辑</strong>
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
