import { useQuery } from "@tanstack/react-query";
import { ChevronDown, ChevronRight, Download, History } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router-dom";

import { api } from "../api/client";
import type { UsageRow } from "../api/types";
import { Badge, EmptyState, ErrorPanel, LoadingLines, Panel } from "../components/ui";
import { formatDateTime, todayShanghai } from "../utils/time";

export function UsagePage() {
  const [from, setFrom] = useState(() => {
    const date = new Date();
    date.setDate(date.getDate() - 6);
    return date.toISOString().slice(0, 10);
  });
  const [to, setTo] = useState(todayShanghai());
  const [expandedProject, setExpandedProject] = useState("");
  const [expandedEngineering, setExpandedEngineering] = useState("");
  const projects = useQuery({
    queryKey: ["usage-projects", from, to],
    queryFn: () => api.usageProjects(from, to),
  });
  const engineering = useQuery({
    queryKey: ["usage-engineering", expandedProject, from, to],
    queryFn: () => api.usageEngineering(expandedProject, from, to),
    enabled: Boolean(expandedProject),
  });
  const records = useQuery({
    queryKey: ["usage-records", expandedEngineering],
    queryFn: () => api.usageRecords(expandedEngineering),
    enabled: Boolean(expandedEngineering),
  });

  return (
    <div className="page usage-page">
      <header className="page-head">
        <div>
          <span className="eyebrow">USAGE LEDGER / DURABLE HISTORY</span>
          <h1>使用统计</h1>
        </div>
        <div className="page-actions">
          <label className="date-range">
            <input type="date" value={from} onChange={(event) => setFrom(event.target.value)} />
            <span>至</span>
            <input type="date" value={to} onChange={(event) => setTo(event.target.value)} />
          </label>
          <a className="button button-secondary button-default" href={`/api/usage/export.csv?from=${from}&to=${to}`}>
            <Download size={14} /> 导出 CSV
          </a>
        </div>
      </header>
      {projects.data?.first_recorded_at ? (
        <p className="data-footnote">统计自 {formatDateTime(projects.data.first_recorded_at)} 起完整；更早且已清理的记录无法补造。</p>
      ) : null}
      {projects.isPending ? <LoadingLines rows={8} /> : null}
      {projects.isError ? <ErrorPanel error={projects.error} onRetry={() => void projects.refetch()} /> : null}
      {projects.data && !projects.data.items.length ? (
        <EmptyState icon={<History size={30} />} title="时间范围内还没有使用记录。" />
      ) : null}
      {projects.data?.items.length ? (
        <Panel className="usage-table-panel">
          <table className="data-table usage-table">
            <thead>
              <tr>
                <th>项目 / 工程</th>
                <th className="numeric">使用</th>
                <th className="numeric">交付</th>
                <th className="numeric">提交</th>
                <th className="numeric">交付率</th>
                <th className="numeric">领取未用</th>
                <th>最近使用</th>
              </tr>
            </thead>
            <tbody>
              {projects.data.items.map((project) => (
                <UsageProjects
                  key={project.project_id}
                  project={project}
                  expanded={expandedProject === project.project_id}
                  onToggle={() => {
                    setExpandedProject((current) => current === project.project_id ? "" : project.project_id);
                    setExpandedEngineering("");
                  }}
                  engineering={expandedProject === project.project_id ? engineering.data?.items ?? [] : []}
                  loading={engineering.isPending}
                  expandedEngineering={expandedEngineering}
                  onEngineering={(id) => setExpandedEngineering((current) => current === id ? "" : id)}
                  records={records.data?.items ?? []}
                  recordsLoading={records.isPending}
                />
              ))}
            </tbody>
          </table>
        </Panel>
      ) : null}
    </div>
  );
}

function UsageProjects({
  project,
  expanded,
  onToggle,
  engineering,
  loading,
  expandedEngineering,
  onEngineering,
  records,
  recordsLoading,
}: {
  project: UsageRow;
  expanded: boolean;
  onToggle: () => void;
  engineering: UsageRow[];
  loading: boolean;
  expandedEngineering: string;
  onEngineering: (id: string) => void;
  records: UsageRow[];
  recordsLoading: boolean;
}) {
  return (
    <>
      <tr className="usage-project-row" onClick={onToggle}>
        <td>
          <button type="button" className="usage-expand" aria-expanded={expanded}>
            {expanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
            <strong>{project.project_name}</strong>
          </button>
        </td>
        <UsageCells row={project} />
      </tr>
      {expanded ? (
        loading && !engineering.length ? (
          <tr><td colSpan={7}><LoadingLines rows={2} /></td></tr>
        ) : engineering.length ? engineering.map((item) => (
          <EngineeringRows
            key={item.engineering_id}
            row={item}
            expanded={expandedEngineering === item.engineering_id}
            onToggle={() => onEngineering(item.engineering_id)}
            records={expandedEngineering === item.engineering_id ? records : []}
            loading={recordsLoading}
          />
        )) : <tr><td colSpan={7} className="empty-inline">该项目暂无工程级记录。</td></tr>
      ) : null}
    </>
  );
}

function EngineeringRows({
  row,
  expanded,
  onToggle,
  records,
  loading,
}: {
  row: UsageRow;
  expanded: boolean;
  onToggle: () => void;
  records: UsageRow[];
  loading: boolean;
}) {
  return (
    <>
      <tr className="usage-engineering-row" onClick={onToggle}>
        <td>
          <button type="button" className="usage-expand">
            {expanded ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
            {row.engineering_name}
          </button>
        </td>
        <UsageCells row={row} />
      </tr>
      {expanded ? loading && !records.length ? (
        <tr><td colSpan={7}><LoadingLines rows={2} /></td></tr>
      ) : records.map((record, index) => (
        <tr key={`${record.task_id || record.claimed_at}-${index}`} className="usage-record-row">
          <td>
            <span className="usage-record-task">{record.task_name || "领取后未建题"}</span>
            <small>{formatDateTime(record.claimed_at || "")}</small>
          </td>
          <td colSpan={4}>
            <Badge tone={outcomeTone(record)}>{outcomeLabel(record)}</Badge>
            {record.failure_kind ? <span className="danger-text"> · {record.failure_kind}</span> : null}
          </td>
          <td colSpan={2}>
            {record.purged_at ? "已清理" : record.task_exists && record.task_id ? (
              <Link to={`/tasks/${record.task_id}`}>查看任务</Link>
            ) : "记录保留"}
          </td>
        </tr>
      )) : null}
    </>
  );
}

function UsageCells({ row }: { row: UsageRow }) {
  return (
    <>
      <td className="numeric mono">{row.used || "–"}</td>
      <td className="numeric mono">{row.delivered || "–"}</td>
      <td className="numeric mono">{row.submitted || "–"}</td>
      <td className="numeric mono">{row.used ? `${Math.round(row.delivery_rate * 100)}%` : "–"}</td>
      <td className="numeric mono">{row.claimed_unused || "–"}</td>
      <td>{row.last_used_at ? formatDateTime(row.last_used_at) : "—"}</td>
    </>
  );
}

function outcomeLabel(row: UsageRow): string {
  if (row.purged_at) return "已清理";
  switch (row.last_outcome || row.outcome) {
    case "submitted": return "已提交";
    case "delivered": return "已交付";
    case "failed": return "失败";
    case "void": return "作废";
    case "released": return "已归还";
    case "claimed": return "领取未用";
    default: return "进行中";
  }
}

function outcomeTone(row: UsageRow): "ok" | "warn" | "danger" | "info" | "idle" {
  if (row.purged_at) return "idle";
  if (row.outcome === "submitted") return "ok";
  if (row.outcome === "failed" || row.outcome === "void") return "danger";
  if (row.outcome === "released" || row.outcome === "claimed") return "warn";
  return "info";
}
