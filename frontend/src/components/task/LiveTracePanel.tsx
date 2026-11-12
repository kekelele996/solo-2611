import { Download, Search } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import { api } from "../../api/client";
import { Badge, StatusDot } from "../ui";

const LIVE_TRACE_KINDS = [
  ["message", "消息"],
  ["command", "命令"],
  ["thinking", "推理"],
  ["error", "错误"],
  ["other", "其他"],
] as const;

type LiveTraceKind = (typeof LIVE_TRACE_KINDS)[number][0];

export function LiveTracePanel({ taskId }: { taskId: string }) {
  const body = useRef<HTMLDivElement>(null);
  const [frames, setFrames] = useState<Array<{ kind: string; text: string }>>([]);
  const [meta, setMeta] = useState<{ stage: string; file: string } | null>(null);
  const [ready, setReady] = useState(false);
  const [loadFailed, setLoadFailed] = useState(false);
  const [filters, setFilters] = useState<Set<LiveTraceKind>>(
    () => new Set(LIVE_TRACE_KINDS.map(([kind]) => kind)),
  );
  const [query, setQuery] = useState("");
  const [follow, setFollow] = useState(true);
  const [connected, setConnected] = useState(false);
  const [lastSyncedAt, setLastSyncedAt] = useState("");

  useEffect(() => {
    let cancelled = false;
    setReady(false);
    setLoadFailed(false);
    api.autopilotLog(taskId).then(
      (data) => {
        if (cancelled) return;
        setFrames(data.lines);
        setMeta({ stage: data.stage, file: data.file });
        setLastSyncedAt(new Date().toISOString());
        setReady(true);
      },
      () => {
        if (!cancelled) setLoadFailed(true);
      },
    );
    return () => {
      cancelled = true;
    };
  }, [taskId]);

  useEffect(() => {
    if (!ready) return;
    const source = new EventSource("/api/events/stream");
    const connectedAt = Date.now();
    source.onopen = () => setConnected(true);
    source.onerror = () => setConnected(false);
    const receive = (event: Event) => {
      try {
        const payload = JSON.parse((event as MessageEvent<string>).data) as {
          task_id?: string;
          at?: string;
          frames?: Array<{ kind: string; text: string }>;
        };
        if (payload.task_id !== taskId || !payload.frames?.length) return;
        const eventAt = payload.at ? Date.parse(payload.at) : Number.NaN;
        // A new EventSource connection replays the bus history. The initial HTTP
        // read already contains those frames, so only append events from now on.
        if (Number.isFinite(eventAt) && eventAt < connectedAt - 1000) return;
        setFrames((current) => [...current, ...payload.frames!].slice(-500));
        setLastSyncedAt(payload.at || new Date().toISOString());
      } catch {
        return;
      }
    };
    source.addEventListener("autopilot.log", receive);
    return () => {
      source.close();
      setConnected(false);
    };
  }, [ready, taskId]);

  useEffect(() => {
    if (follow) body.current?.scrollTo?.({ top: body.current.scrollHeight });
  }, [follow, frames]);

  const visible = useMemo(() => {
    const term = query.trim().toLowerCase();
    return frames.filter((frame) => {
      const kind = (LIVE_TRACE_KINDS.some(([value]) => value === frame.kind) ? frame.kind : "other") as LiveTraceKind;
      return filters.has(kind) && (!term || frame.text.toLowerCase().includes(term));
    });
  }, [filters, frames, query]);

  if (loadFailed && !frames.length) return null;

  return (
    <section className="live-trace" aria-label="运行轨迹">
      <header className="live-trace-head">
        <div>
          <StatusDot state={connected ? "running" : "queued"} label={connected ? "实时接收" : "正在连接"} pulse={connected} />
          <strong>运行轨迹</strong>
          <Badge tone={connected ? "ok" : "warn"}>{frames.length} 条</Badge>
        </div>
        <span className="mono">
          {meta?.stage ? `阶段 ${meta.stage}` : "当前阶段"}
          {lastSyncedAt ? ` · ${new Date(lastSyncedAt).toLocaleTimeString("zh-CN", { hour12: false })}` : ""}
        </span>
        {meta?.file ? (
          <a href={`/api/autopilot/${encodeURIComponent(taskId)}/log?download=1`}>
            <Download size={12} /> 下载原始 JSONL
          </a>
        ) : null}
      </header>
      <div className="live-trace-toolbar">
        <label className="search-box">
          <Search size={14} />
          <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索运行轨迹" />
        </label>
        {LIVE_TRACE_KINDS.map(([kind, label]) => (
          <button
            type="button"
            key={kind}
            data-active={filters.has(kind) || undefined}
            onClick={() =>
              setFilters((current) => {
                const next = new Set(current);
                if (next.has(kind)) next.delete(kind);
                else next.add(kind);
                return next;
              })
            }
          >
            {label}
          </button>
        ))}
        <span>{visible.length} / {frames.length}</span>
      </div>
      <div
        className="live-trace-body"
        ref={body}
        onScroll={(event) => {
          const element = event.currentTarget;
          setFollow(element.scrollHeight - element.scrollTop - element.clientHeight < 24);
        }}
      >
        {visible.length ? visible.map((frame, index) => (
          <div key={`${index}-${frame.kind}`} data-kind={frame.kind}>
            <span>{frame.kind}</span>
            <pre>{frame.text}</pre>
          </div>
        )) : (
          <p className="empty-inline">{frames.length ? "当前筛选没有匹配的轨迹。" : "等待 ChatGPT 或运行容器产生第一条轨迹…"}</p>
        )}
      </div>
      {!follow ? <button type="button" className="back-to-latest" onClick={() => setFollow(true)}>回到最新</button> : null}
      {meta?.file ? <p className="live-trace-file mono">{meta.file}</p> : null}
    </section>
  );
}
