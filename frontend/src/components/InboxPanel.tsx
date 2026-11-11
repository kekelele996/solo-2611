import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, ClipboardCheck, Factory, Inbox, LogIn, Trash2 } from "lucide-react";
import type { ReactNode } from "react";
import { Link } from "react-router-dom";

import { api } from "../api/client";

type InboxItem = {
  key: string;
  tone: "danger" | "warn" | "info";
  icon: ReactNode;
  title: string;
  detail: string;
  to: string;
  action: string;
};

export function InboxPanel() {
  const pilot = useQuery({ queryKey: ["autopilot"], queryFn: api.autopilot, refetchInterval: 5000 });
  const audits = useQuery({ queryKey: ["audits", "pending"], queryFn: () => api.audits("pending"), refetchInterval: 5000 });
  const intake = useQuery({ queryKey: ["intake"], queryFn: api.intake, refetchInterval: 5000 });
  const status = useQuery({ queryKey: ["system-status"], queryFn: api.systemStatus, refetchInterval: 5000 });
  const trash = useQuery({ queryKey: ["trash"], queryFn: api.trash, refetchInterval: 30_000 });
  const failed = (pilot.data?.jobs ?? []).filter((job) => job.stage === "failed");
  const expiring = (trash.data?.items ?? []).filter(
    (item) => item.remaining_days !== null && item.remaining_days !== undefined && item.remaining_days <= 2,
  );
  const items: InboxItem[] = [];
  if (pilot.data?.paused) {
    items.push({
      key: "breaker",
      tone: "danger",
      icon: <AlertTriangle size={16} />,
      title: pilot.data.paused_by_breaker ? "自动交付已熔断" : "自动交付已暂停",
      detail: pilot.data.breaker.kind ? `类别：${pilot.data.breaker.kind}` : "生产线不再拉起新阶段",
      to: "/production",
      action: "查看原因",
    });
  }
  if (audits.data?.total) {
    items.push({
      key: "audit",
      tone: "warn",
      icon: <ClipboardCheck size={16} />,
      title: `待抽检 ${audits.data.total} 题`,
      detail: "抽检通过前，被抽中的任务不能进入提交批次",
      to: "/audit",
      action: "开始抽检",
    });
  }
  if (intake.data?.plan?.status === "paused") {
    items.push({
      key: "intake",
      tone: "warn",
      icon: <Factory size={16} />,
      title: "生产计划已暂停",
      detail: intake.data.plan.pause_reason || "等待人工继续",
      to: "/production",
      action: "查看计划",
    });
  }
  if (status.data?.offline) {
    items.push({
      key: "source",
      tone: "danger",
      icon: <LogIn size={16} />,
      title: "题源登录已过期或离线",
      detail: "历史任务仍可查看，自动领取已停止",
      to: "/source",
      action: "重新登录",
    });
  }
  if (failed.length) {
    items.push({
      key: "failed",
      tone: "warn",
      icon: <AlertTriangle size={16} />,
      title: `${failed.length} 题自动交付失败`,
      detail: "每题都已有结构化失败类别，可从失败阶段续跑",
      to: "/production",
      action: "处理失败",
    });
  }
  if (expiring.length) {
    items.push({
      key: "trash",
      tone: "warn",
      icon: <Trash2 size={16} />,
      title: `${expiring.length} 个回收站条目即将到期`,
      detail: `保留期 ${trash.data?.retention_days ?? 0} 天；0 表示不自动清理`,
      to: "/system#trash",
      action: "打开回收站",
    });
  }

  if (!items.length) {
    return (
      <div className="inbox-panel is-empty">
        <span><Inbox size={15} /> 没有需要你处理的事</span>
      </div>
    );
  }
  return (
    <section className="inbox-panel" aria-labelledby="inbox-title">
      <header>
        <h2 id="inbox-title">待我处理 <span>{items.length}</span></h2>
      </header>
      <div className="inbox-items">
        {items.map((item) => (
          <div key={item.key} className={`inbox-item inbox-${item.tone}`}>
            {item.icon}
            <div>
              <strong>{item.title}</strong>
              <span>{item.detail}</span>
            </div>
            <Link to={item.to}>{item.action}</Link>
          </div>
        ))}
      </div>
    </section>
  );
}
