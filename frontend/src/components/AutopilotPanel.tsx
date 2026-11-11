import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ScrollText, Square } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router-dom";

import { api } from "../api/client";
import type { AutopilotJob } from "../api/types";
import { errorText } from "../utils/display";
import { useToast } from "./toast-context";
import { Button, EmptyState, Modal, Panel, StatusDot } from "./ui";

const STAGE_STATE: Record<string, string> = { done: "exported", failed: "blocked", stopped: "archived" };
const ATTEMPT_LABELS: Record<string, string> = { authoring: "出题", running: "运行", delivering: "交付" };

/** 自动交付的监看面板：每题走到哪一步、Agent 在干什么，随时能停。没有任务时不占位。 */
export function AutopilotPanel() {
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const [logFor, setLogFor] = useState<AutopilotJob | null>(null);
  const pilot = useQuery({ queryKey: ["autopilot"], queryFn: api.autopilot, refetchInterval: 3000 });
  const stop = useMutation({
    mutationFn: (ids: string[]) => api.autopilotStop(ids),
    onSuccess: (result) => {
      toast(result.stopped.length ? `已停止 ${result.stopped.length} 题的自动交付` : "没有进行中的自动交付", "info");
      void queryClient.invalidateQueries();
    },
    onError: (error) => toast(errorText(error), "error"),
  });
  const jobs = pilot.data?.jobs ?? [];
  if (!jobs.length) return null;
  const active = pilot.data?.active ?? 0;

  return (
    <Panel
      className="autopilot-panel"
      title={`自动交付 · 进行中 ${active} 题`}
      actions={
        active ? (
          <Button size="small" variant="ghost" loading={stop.isPending} onClick={() => stop.mutate([])}>
            <Square size={12} /> 全部停止
          </Button>
        ) : undefined
      }
    >
      <ul className="autopilot-rows">
        {jobs.map((job) => (
          <li key={job.task_id} className={job.stage === "failed" ? "is-failed" : undefined}>
            <StatusDot
              state={job.active ? "running" : (STAGE_STATE[job.stage] ?? job.stage)}
              label={job.stage_label}
              pulse={job.agent_running || job.stage === "running"}
            />
            <Link className="ellipsis" to={`/tasks/${job.task_id}`} title={job.task_name}>
              {job.task_name}
            </Link>
            <span className="ellipsis autopilot-detail" title={job.detail}>
              {job.detail}
            </span>
            <span className="mono autopilot-attempts">
              {Object.entries(job.attempts)
                .map(([stage, count]) => `${ATTEMPT_LABELS[stage] ?? stage}×${count}`)
                .join(" ") || "—"}
            </span>
            <Button size="small" variant="ghost" disabled={!job.log} onClick={() => setLogFor(job)}>
              <ScrollText size={12} /> Agent 日志
            </Button>
            {job.active ? (
              <Button size="small" variant="ghost" onClick={() => stop.mutate([job.task_id])}>
                停止
              </Button>
            ) : (
              <span />
            )}
          </li>
        ))}
      </ul>
      <AgentLog job={logFor} onClose={() => setLogFor(null)} />
    </Panel>
  );
}

function AgentLog({ job, onClose }: { job: AutopilotJob | null; onClose: () => void }) {
  const log = useQuery({
    queryKey: ["autopilot-log", job?.task_id],
    queryFn: () => api.autopilotLog(job?.task_id ?? ""),
    enabled: Boolean(job),
    refetchInterval: 3000,
  });
  return (
    <Modal open={Boolean(job)} title={`Agent 日志 · ${job?.task_name ?? ""}`} onClose={onClose}>
      {log.data?.lines.length ? (
        <ol className="autopilot-log">
          {log.data.lines.map((line, index) => (
            <li key={index} data-kind={line.kind}>
              {line.text}
            </li>
          ))}
        </ol>
      ) : (
        <EmptyState title={log.isError ? errorText(log.error) : "Agent 还没有输出。"} />
      )}
      {log.data?.file ? <p className="form-note mono">{log.data.file}</p> : null}
    </Modal>
  );
}
