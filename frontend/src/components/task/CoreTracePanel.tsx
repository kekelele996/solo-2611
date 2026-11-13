import { useQuery } from "@tanstack/react-query";
import {
  Activity,
  ChevronRight,
  Clock3,
  Download,
  FilePenLine,
  Search,
  TerminalSquare,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";

import { api } from "../../api/client";
import type { CoreTraceKind, CoreTraceStep } from "../../api/types";
import { formatClock } from "../../utils/time";
import { Badge, ErrorPanel, StatusDot } from "../ui";

const KINDS: Array<[CoreTraceKind, string]> = [
  ["command", "命令"],
  ["edit", "改码"],
  ["read", "读取"],
  ["search", "检索"],
  ["agent", "子任务"],
  ["network", "联网"],
  ["other", "其他"],
];

const KIND_LABEL = Object.fromEntries(KINDS) as Record<CoreTraceKind, string>;

export function CoreTracePanel({ taskId }: { taskId: string }) {
  const body = useRef<HTMLDivElement>(null);
  const [query, setQuery] = useState("");
  const [failedOnly, setFailedOnly] = useState(false);
  const [expanded, setExpanded] = useState<number | null>(null);
  const [follow, setFollow] = useState(true);
  const [filters, setFilters] = useState<Set<CoreTraceKind>>(
    () => new Set(KINDS.map(([kind]) => kind)),
  );
  const trace = useQuery({
    queryKey: ["core-trace", taskId],
    queryFn: () => api.coreTrace(taskId),
    refetchInterval: 2000,
  });

  const rows = useMemo(() => {
    const term = query.trim().toLowerCase();
    return (trace.data?.steps ?? []).filter((step) => {
      const haystack = `${step.tool} ${step.summary} ${step.note} ${step.target} ${step.output}`.toLowerCase();
      return filters.has(step.kind)
        && (!failedOnly || step.status === "failed")
        && (!term || haystack.includes(term));
    });
  }, [failedOnly, filters, query, trace.data?.steps]);

  const activity = useMemo(() => buildActivity(trace.data?.steps ?? []), [trace.data?.steps]);

  useEffect(() => {
    if (follow) body.current?.scrollTo?.({ top: body.current.scrollHeight });
  }, [follow, rows]);

  useEffect(() => {
    setExpanded(null);
  }, [taskId]);

  if (trace.isError && !trace.data) {
    const code = (trace.error as { code?: string }).code;
    if (code !== "CORE_TRACE_NOT_AVAILABLE") {
      return <ErrorPanel error={trace.error} onRetry={() => void trace.refetch()} />;
    }
  }

  const data = trace.data;
  const live = data?.source.live === true;
  const sourceLabel = data?.source.mode === "final" ? "最终轨迹" : live ? "实时写入" : "等待轨迹";
  const modified = data?.modified_files ?? [];

  return (
    <section className="core-trace" aria-label="Claude Code 核心执行轨迹">
      <header className="core-trace-head">
        <div className="core-trace-title">
          <span className="eyebrow">CORE CODE / TOOL EXECUTION</span>
          <div>
            <StatusDot state={live ? "running" : data ? "done" : "queued"} label={sourceLabel} pulse={live} />
            <strong>Claude Code 执行轨迹</strong>
            <Badge tone={data?.counts.failed ? "danger" : live ? "ok" : "idle"}>
              {data?.counts.total ?? 0} 个动作
            </Badge>
          </div>
          <p>只展示核心代码工具调用，不混入 ChatGPT 对话、思考过程或自动交付日志。</p>
        </div>
        <div className="core-trace-head-meta">
          <span className="mono">
            {data?.source.models.join(" / ") || "等待模型信息"}
            {data?.source.updated_at ? ` · ${formatClock(data.source.updated_at)}` : ""}
          </span>
          {data ? (
            <a href={`/api/tasks/${encodeURIComponent(taskId)}/core-trace/raw`}>
              <Download size={12} /> 下载核心 JSONL
            </a>
          ) : null}
        </div>
      </header>

      {data ? (
        <>
          <div className="core-trace-overview">
            <div className="core-trace-metrics">
              <TraceMetric icon={<Activity size={14} />} label="动作" value={data.counts.total} />
              <TraceMetric icon={<TerminalSquare size={14} />} label="命令" value={data.counts.commands} />
              <TraceMetric icon={<FilePenLine size={14} />} label="改码" value={data.counts.edits} />
              <TraceMetric
                icon={<Clock3 size={14} />}
                label="失败"
                value={data.counts.failed}
                danger={data.counts.failed > 0}
              />
            </div>
            <div className="core-trace-activity" aria-label="执行活跃度">
              <div>
                <span>执行活跃度</span>
                <small>{data.counts.running ? `${data.counts.running} 个动作进行中` : "按动作发生时间分布"}</small>
              </div>
              <div className="core-trace-bars">
                {activity.map((value, index) => (
                  <i
                    key={index}
                    style={{ height: `${Math.max(8, value * 100)}%` }}
                    data-empty={value === 0 || undefined}
                  />
                ))}
              </div>
            </div>
            <div className="core-trace-files">
              <span>修改文件</span>
              {modified.length ? modified.slice(0, 5).map((path) => (
                <code key={path} title={path}>{shortPath(path)}</code>
              )) : <small>尚未修改文件</small>}
              {modified.length > 5 ? <small>另有 {modified.length - 5} 个</small> : null}
            </div>
          </div>

          <div className="core-trace-toolbar">
            <label className="search-box">
              <Search size={14} />
              <input
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="搜索命令、文件或输出"
              />
            </label>
            {KINDS.map(([kind, label]) => (
              <button
                type="button"
                key={kind}
                data-active={filters.has(kind) || undefined}
                data-kind={kind}
                onClick={() =>
                  setFilters((current) => {
                    const next = new Set(current);
                    if (next.has(kind)) next.delete(kind);
                    else next.add(kind);
                    return next;
                  })
                }
              >
                {label} {data.counts.by_kind[kind] || ""}
              </button>
            ))}
            <button
              type="button"
              className="core-trace-failed-filter"
              data-active={failedOnly || undefined}
              disabled={!data.counts.failed && !failedOnly}
              onClick={() => setFailedOnly((value) => !value)}
            >
              只看失败 {data.counts.failed || ""}
            </button>
            <span>{rows.length} / {data.steps.length}</span>
          </div>
        </>
      ) : null}

      <div
        className="core-trace-body"
        ref={body}
        onScroll={(event) => {
          const element = event.currentTarget;
          setFollow(element.scrollHeight - element.scrollTop - element.clientHeight < 28);
        }}
      >
        {trace.isPending && !data ? (
          <p className="empty-inline">正在读取 Claude Code 核心轨迹…</p>
        ) : rows.length ? (
          <ol className="core-trace-list">
            {rows.map((step) => (
              <TraceEvent
                key={`${step.tool_use_id}-${step.no}`}
                step={step}
                expanded={expanded === step.no}
                onToggle={() => setExpanded((value) => value === step.no ? null : step.no)}
              />
            ))}
          </ol>
        ) : (
          <p className="empty-inline">
            {data?.steps.length
              ? "当前筛选没有匹配的核心执行动作。"
              : "等待 Claude Code 写入第一条命令、读取或改码动作…"}
          </p>
        )}
      </div>
      {!follow ? (
        <button
          type="button"
          className="core-trace-latest"
          onClick={() => {
            setFollow(true);
            body.current?.scrollTo?.({ top: body.current.scrollHeight });
          }}
        >
          回到最新
        </button>
      ) : null}
      {data?.source.path ? <p className="core-trace-file mono">{data.source.path}</p> : null}
    </section>
  );
}

function TraceMetric({
  icon,
  label,
  value,
  danger = false,
}: {
  icon: ReactNode;
  label: string;
  value: number;
  danger?: boolean;
}) {
  return (
    <div className={danger ? "is-danger" : ""}>
      {icon}
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function TraceEvent({
  step,
  expanded,
  onToggle,
}: {
  step: CoreTraceStep;
  expanded: boolean;
  onToggle: () => void;
}) {
  const outputTruncated = step.output_chars > step.output.length;
  return (
    <li className={`core-trace-event ${expanded ? "is-open" : ""}`} data-kind={step.kind} data-status={step.status}>
      <span className="core-trace-node" aria-hidden="true" />
      <button type="button" className="core-trace-event-head" aria-expanded={expanded} onClick={onToggle}>
        <ChevronRight size={13} />
        <span className="core-trace-no mono">#{step.no}</span>
        <Badge tone={step.status === "failed" ? "danger" : step.status === "running" ? "warn" : "idle"}>
          {KIND_LABEL[step.kind]} · {step.tool}
        </Badge>
        <span className="core-trace-summary" title={step.summary}>{step.summary || "(无摘要)"}</span>
        <span className="core-trace-status">
          {step.status === "failed" ? "失败" : step.status === "running" ? "进行中" : "完成"}
        </span>
        <time>{formatClock(step.ts)}</time>
      </button>
      <div className="core-trace-event-sub">
        <span>{step.note || KIND_LABEL[step.kind]}</span>
        {step.duration_ms !== null ? <span>{formatElapsed(step.duration_ms)}</span> : null}
        {step.target ? <code title={step.target}>{shortPath(step.target)}</code> : null}
      </div>
      {expanded ? (
        <div className="core-trace-detail">
          <TraceBlock label="输入">{JSON.stringify(step.input, null, 2)}</TraceBlock>
          <TraceBlock label={outputTruncated ? `输出 · 已显示前 ${step.output.length} 字` : "输出"}>
            {step.output || "(无输出)"}
          </TraceBlock>
        </div>
      ) : null}
    </li>
  );
}

function TraceBlock({ label, children }: { label: string; children: string }) {
  return (
    <div className="core-trace-code">
      <span>{label}</span>
      <pre>{children}</pre>
    </div>
  );
}

function buildActivity(steps: CoreTraceStep[]): number[] {
  const bins = Array.from({ length: 24 }, () => 0);
  if (!steps.length) return bins;
  const times = steps.map((step) => Date.parse(step.ts)).filter(Number.isFinite);
  if (times.length !== steps.length) {
    steps.forEach((_, index) => {
      const bin = Math.min(23, Math.floor(index * 24 / steps.length));
      bins[bin] += 1;
    });
  } else {
    const start = Math.min(...times);
    const end = Math.max(...times);
    const span = Math.max(1, end - start);
    times.forEach((time) => {
      const bin = Math.min(23, Math.max(0, Math.floor(((time - start) / span) * 23)));
      bins[bin] += 1;
    });
  }
  const peak = Math.max(...bins, 1);
  return bins.map((value) => value / peak);
}

function formatElapsed(milliseconds: number): string {
  if (milliseconds < 1000) return `${milliseconds}ms`;
  const seconds = milliseconds / 1000;
  if (seconds < 60) return `${seconds.toFixed(seconds < 10 ? 1 : 0)}s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes}m${Math.round(seconds % 60)}s`;
}

function shortPath(path: string): string {
  const parts = path.split("/").filter(Boolean);
  return parts.length > 2 ? `…/${parts.slice(-2).join("/")}` : path;
}
