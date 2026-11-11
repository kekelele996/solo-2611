import { useEffect } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useToast } from "../components/toast-context";

export function useEvents(): void {
  const queryClient = useQueryClient();
  const { toast } = useToast();

  useEffect(() => {
    const source = new EventSource("/api/events/stream");
    const refresh = () => {
      void queryClient.invalidateQueries();
    };
    source.addEventListener("task.updated", refresh);
    source.addEventListener("queue.updated", refresh);
    source.addEventListener("run.log", refresh);
    source.addEventListener("system", refresh);
    source.addEventListener("export.updated", refresh);
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
    return () => source.close();
  }, [queryClient, toast]);
}
