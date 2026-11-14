import { useQuery } from "@tanstack/react-query";
import { Filter, RefreshCw, ScrollText } from "lucide-react";
import { useMemo, useState } from "react";

import { api } from "../api/client";
import type { DispatchLogRow } from "../api/types";
import { Button, EmptyState, ErrorPanel, LoadingLines, Panel } from "../components/ui";
import { formatDateTime } from "../utils/time";

const FAMILIES = ["", "dispatch", "gate", "hold", "probe", "retry", "guard", "capacity", "limit", "recovery", "watchdog", "refill", "intent"];
const LEVELS = ["", "info", "warn", "error"];

export function DispatchLogPage() {
  const [family, setFamily] = useState("");
  const [level, setLevel] = useState("");
  const [taskId, setTaskId] = useState("");
  const logs = useQuery({
    queryKey: ["dispatch-log", family, level, taskId],
    queryFn: () => api.dispatchLog({ family, level, taskId, limit: 500 }),
    refetchInterval: 3000,
  });
  const rows = useMemo(() => [...(logs.data?.items ?? [])].reverse(), [logs.data?.items]);

  return (
    <div className="page dispatch-log-page">
      <header className="page-head">
        <div>
          <span className="eyebrow">DISPATCH / STRUCTURED EVENTS</span>
          <h1>调度日志</h1>
        </div>
        <Button size="small" onClick={() => void logs.refetch()}><RefreshCw size={13} /> 刷新</Button>
      </header>
      <section className="dispatch-log-filters" aria-label="日志筛选">
        <label><Filter size={14} /><select value={family} onChange={(event) => setFamily(event.target.value)}><option value="">全部事件族</option>{FAMILIES.filter(Boolean).map((value) => <option key={value}>{value}</option>)}</select></label>
        <label><select value={level} onChange={(event) => setLevel(event.target.value)}><option value="">全部级别</option>{LEVELS.filter(Boolean).map((value) => <option key={value}>{value}</option>)}</select></label>
        <label className="dispatch-log-task"><input value={taskId} onChange={(event) => setTaskId(event.target.value)} placeholder="按任务 ID 串联" /></label>
      </section>
      {logs.isPending ? <LoadingLines rows={10} /> : logs.isError ? <ErrorPanel error={logs.error} onRetry={() => void logs.refetch()} /> : rows.length ? (
        <Panel title={`${rows.length} 条事件`} className="dispatch-log-panel">
          <ol className="dispatch-log-rows">
            {rows.map((row) => <LogRow key={row.id} row={row} />)}
          </ol>
        </Panel>
      ) : (
        <EmptyState icon={<ScrollText size={28} />} title="当前筛选下没有调度事件。" />
      )}
    </div>
  );
}

function LogRow({ row }: { row: DispatchLogRow }) {
  const [open, setOpen] = useState(false);
  return (
    <li data-level={row.level} data-open={open || undefined}>
      <button type="button" onClick={() => setOpen((value) => !value)}>
        <time>{formatDateTime(row.at, true)}</time>
        <span className="dispatch-log-family">{row.family}</span>
        <span className="dispatch-log-action">{row.action}</span>
        <span className="dispatch-log-message">{row.message}</span>
        <span className="mono">{row.task_id?.slice(0, 8) || "—"}</span>
      </button>
      {open ? <pre>{JSON.stringify({ actor: row.actor, details: row.details }, null, 2)}</pre> : null}
    </li>
  );
}
