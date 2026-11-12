import { useEffect } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useToast } from "../components/toast-context";

/**
 * 事件只是「提前刷新」的信号，各查询本身都有轮询兜底。
 * 运行中的后端每秒会推几十条事件，逐条全量失效会让每个页面持续重拉、重渲染，
 * 所以这里合并成每 FLUSH_MS 至多一次；日志类事件只刷新对应的日志查询。
 */
const FLUSH_MS = 1000;

const BROAD_EVENTS = [
  "task.updated",
  "queue.updated",
  "system",
  "export.updated",
  "autopilot.updated",
  "autopilot.breaker",
  "dispatch.updated",
  "dispatch.hold",
  "dispatch.limit",
  "guard.hit",
  "audit.updated",
  "intake.updated",
  "batch.updated",
  "trash.updated",
];

const SCOPED_EVENTS: Record<string, string> = {
  "run.log": "run",
  "dispatch.log": "dispatch-log",
};

export function useEvents(): void {
  const queryClient = useQueryClient();
  const { toast } = useToast();

  useEffect(() => {
    const source = new EventSource("/api/events/stream");
    let timer: number | undefined;
    let broad = false;
    const scoped = new Set<string>();
    const flush = () => {
      timer = undefined;
      if (broad) {
        void queryClient.invalidateQueries();
      } else {
        for (const key of scoped) void queryClient.invalidateQueries({ queryKey: [key] });
      }
      broad = false;
      scoped.clear();
    };
    const schedule = () => {
      if (timer === undefined) timer = window.setTimeout(flush, FLUSH_MS);
    };
    const refreshAll = () => {
      broad = true;
      schedule();
    };
    for (const name of BROAD_EVENTS) source.addEventListener(name, refreshAll);
    for (const [name, key] of Object.entries(SCOPED_EVENTS)) {
      source.addEventListener(name, () => {
        scoped.add(key);
        schedule();
      });
    }
    source.addEventListener("toast", (event) => {
      try {
        const payload = JSON.parse((event as MessageEvent<string>).data) as {
          level?: "info" | "warn" | "error" | "ok";
          message?: string;
        };
        if (payload.message) toast(payload.message, payload.level ?? "info");
      } catch {
        return;
      }
    });
    return () => {
      if (timer !== undefined) window.clearTimeout(timer);
      source.close();
    };
  }, [queryClient, toast]);
}
