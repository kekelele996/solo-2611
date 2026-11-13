import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CircleStop, Play, RotateCw, ShieldAlert } from "lucide-react";

import { api } from "../../api/client";
import type { SessionRound, TaskDetail } from "../../api/types";
import { useToast } from "../toast-context";
import { Badge, Button } from "../ui";
import { errorText } from "../../utils/display";
import { formatDateTime } from "../../utils/time";

const TERMINAL = new Set(["accepted", "aborted", "start_failed"]);
const KIND_LABEL: Record<string, string> = {
  rate_limited: "限流",
  gateway: "网关错误",
  auth: "鉴权",
  quota: "额度不足",
  model_mismatch: "模型不匹配",
  truncated: "回复截断",
  incomplete: "没有做完",
  empty: "空回复",
  unresponsive: "继续后无响应",
  prompt_delivery_timeout: "题面未送达",
  prompt_delivery_failed: "题面补发失败",
  tmux_session_missing: "终端会话丢失",
  none: "正常结束",
};

export function SessionPanel({
  task,
  onAbort,
}: {
  task: TaskDetail;
  onAbort: () => void;
}) {
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const state = task.state as {
    sessionRun?: {
      mode?: string;
      status?: string;
      attemptIndex?: number;
      continues?: { used?: number; max?: number };
    };
  };
  const hasManagedSession = Boolean(state.sessionRun);
  const session = useQuery({
    queryKey: ["session", task.id],
    queryFn: () => api.session(task.id),
    enabled: hasManagedSession,
    refetchInterval: (query) =>
      TERMINAL.has(String(query.state.data?.status ?? state.sessionRun?.status ?? "")) ? false : 2500,
  });
  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ["session", task.id] });
    void queryClient.invalidateQueries({ queryKey: ["task", task.id] });
  };
  const resume = useMutation({
    mutationFn: () => api.resumeSession(task.id),
    onSuccess: () => {
      toast("会话已接回，继续次数不变", "ok");
      refresh();
    },
    onError: (error) => toast(errorText(error), "error"),
  });
  const sendContinue = useMutation({
    mutationFn: () => api.continueSession(task.id),
    onSuccess: (result) => {
      toast(result.sent ? "已发送“继续”" : "继续请求已排队", "ok");
      refresh();
    },
    onError: (error) => toast(errorText(error), "error"),
  });

  if (!hasManagedSession) return null;
  const status = session.data;
  const used = status?.continues.used ?? state.sessionRun?.continues?.used ?? 0;
  const max = status?.continues.max ?? state.sessionRun?.continues?.max ?? 2;
  const remaining = Math.max(0, max - used);
  const current = status?.status ?? state.sessionRun?.status ?? "unknown";
  const terminal = TERMINAL.has(current);
  const blocked = Boolean(status?.blocked);
  const delivery = status?.promptDelivery;
  const repairing = Boolean(
    delivery &&
      delivery.status !== "confirmed" &&
      delivery.repairCount > 0,
  );
  const startFailure = current === "start_failed" ? status?.error || "启动失败，等待重新排队" : "";
  const rounds = status?.rounds ?? [];

  return (
    <section className={`session-panel ${blocked ? "is-blocked" : terminal ? "is-terminal" : ""}`} aria-label="会话进度">
      <header className="session-panel-head">
        <div>
          <span className="eyebrow">MANAGED SESSION</span>
          <strong>
            第 {status?.round ?? rounds.length ?? 0} 轮 · 已继续 {used}/{max}
          </strong>
        </div>
        <div className="session-panel-status">
          <Badge tone={terminal && current === "accepted" ? "ok" : blocked ? "warn" : "info"}>
            {statusLabel(current)}
          </Badge>
          <span className={status?.supervisorAlive ? "is-ok-text" : "is-warn-text"}>
            监督器{status?.supervisorAlive ? "在线" : "离线"}
          </span>
        </div>
      </header>

      {blocked ? (
        <div className="session-alert">
          <ShieldAlert size={16} />
          <span>
            等待人工处理：{KIND_LABEL[status?.blocked?.kind ?? ""] ?? status?.blocked?.kind}
            {status?.blocked?.until ? ` · 截止 ${formatDateTime(status.blocked.until)}` : ""}
          </span>
        </div>
      ) : null}
      {!blocked && (repairing || startFailure) ? (
        <div className="session-alert">
          <ShieldAlert size={16} />
          <span>
            {startFailure ||
              `题面尚未确认送达，已自动补发 ${delivery?.repairCount ?? 0}/${delivery?.maxRepairCount ?? 4}`}
          </span>
        </div>
      ) : null}

      <ol className="session-rounds">
        {rounds.map((round) => (
          <SessionRoundRow key={`${round.no}-${round.endedAt ?? round.startedAt}`} round={round} />
        ))}
        {!rounds.length ? <li className="session-round-empty">等待第一轮结束</li> : null}
      </ol>

      <footer className="session-panel-foot">
        <span>
          {terminal
            ? current === "accepted"
              ? "会话已接受，可进入产物复核"
              : current === "start_failed"
                ? "题面未送达，调度器会按启动失败重新排队"
                : "会话已终止，需换题"
            : `剩余 ${formatRemaining(status?.remainingSeconds ?? 0)}`}
        </span>
        <div className="row-actions">
          {!terminal ? (
            <>
              <Button
                size="small"
                variant="ghost"
                loading={resume.isPending}
                onClick={() => resume.mutate()}
              >
                <RotateCw size={13} /> 接回
              </Button>
              <Button
                size="small"
                disabled={remaining <= 0}
                loading={sendContinue.isPending}
                onClick={() => {
                  if (
                    window.confirm(
                      `此操作会消耗 1 次继续机会（剩余 ${remaining} 次），只发送两个字“继续”。`,
                    )
                  ) {
                    sendContinue.mutate();
                  }
                }}
              >
                <Play size={13} /> 继续
              </Button>
              <Button size="small" variant="danger" onClick={onAbort}>
                <CircleStop size={13} /> 放弃
              </Button>
            </>
          ) : null}
        </div>
      </footer>
    </section>
  );
}

function SessionRoundRow({ round }: { round: SessionRound }) {
  const kind = KIND_LABEL[round.kind] ?? round.kind ?? "未知";
  return (
    <li className={`session-round kind-${round.kind || "none"}`}>
      <span className="session-round-no">轮 {round.no}</span>
      <div>
        <strong>{kind}</strong>
        <p>
          {round.detail || (round.kind === "none" ? "模型正常结束" : "等待处理")}
          {round.toolUses ? ` · ${round.toolUses} 次工具调用` : ""}
        </p>
      </div>
      <span className="session-round-result">
        {round.continued ? "已继续" : round.verdict === "complete" ? "完成" : round.verdict}
      </span>
    </li>
  );
}

function statusLabel(status: string): string {
  return (
    {
      launching: "启动中",
      prompt_delivery: "题面发送确认中",
      prompt_sent: "题面已发送",
      running: "运行中",
      continuing: "等待继续",
      holding: "等待处理",
      manual_input: "需要人工输入",
      closing: "收尾中",
      accepted: "已接受",
      aborted: "已终止",
      session_lost: "会话丢失",
      start_failed: "启动失败",
    }[status] ?? status
  );
}

function formatRemaining(seconds: number): string {
  if (!seconds) return "0 分钟";
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.ceil((seconds % 3600) / 60);
  return hours ? `${hours} 小时 ${minutes} 分` : `${minutes} 分钟`;
}
