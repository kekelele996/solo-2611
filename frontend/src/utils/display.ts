export function formatNames(values: unknown): string {
  if (!Array.isArray(values)) return "";
  return values
    .map((value) => {
      if (typeof value === "string") return value;
      if (value && typeof value === "object" && "name" in value) {
        const name = (value as { name?: unknown }).name;
        return typeof name === "string" ? name : "";
      }
      return "";
    })
    .filter(Boolean)
    .join(" / ");
}

export type Tone = "ok" | "warn" | "danger" | "info" | "idle";

const STATUS_LABELS: Record<string, string> = {
  authored: "待出题",
  prompt_ok: "待推快照",
  ready: "可运行",
  queued: "排队中",
  running: "运行中",
  ran: "待录屏",
  recorded: "待写 Feedback",
  reviewed: "待导出",
  exported: "已导出",
  archived: "已归档",
  aborted: "已放弃",
  blocked: "卡住",
  orphan: "状态丢失",
};

export const STATUS_FILTERS: Array<[string, string]> = [
  ["", "全部状态"],
  ...Object.entries(STATUS_LABELS),
];

/** 状态的中文名；未知状态原样返回，不猜。 */
export function statusLabel(status: string | null | undefined): string {
  if (!status) return "—";
  return STATUS_LABELS[status] ?? status;
}

export function statusTone(state: string): Tone {
  if (state === "running" || state === "done" || state === "exported" || state === "ready") return "ok";
  if (state === "blocked" || state === "failed" || state === "orphan") return "danger";
  if (state === "ran" || state === "recorded" || state === "reviewed" || state === "prompt_ok") return "warn";
  if (state === "queued") return "info";
  return "idle";
}

/** 把步骤证据压成一眼能认的短形式；完整值仍放在 title 里。 */
export function shortDetail(detail: string | null | undefined): string {
  const value = (detail ?? "").trim();
  if (!value) return "";
  if (/^[0-9a-f]{40}$/i.test(value)) return value.slice(0, 8);
  if (/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value)) return value.slice(0, 8);
  const commit = /^https?:\/\/.+\/commit\/([0-9a-f]{7,40})$/i.exec(value);
  if (commit) return `已推送 ${commit[1].slice(0, 7)}`;
  const stat = /^(\d+) files? changed(?:, (\d+) insertions?\(\+\))?(?:, (\d+) deletions?\(-\))?$/.exec(value);
  if (stat) return `${stat[1]} 文件 +${stat[2] ?? 0} −${stat[3] ?? 0}`;
  if (value.startsWith("/")) return value.split("/").pop() ?? value;
  return value;
}

/** 秒 → 34m12s / 1h05m，用于「已用时长」。 */
export function formatDuration(totalSeconds: number): string {
  const seconds = Math.max(0, Math.round(totalSeconds));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (hours) return `${hours}h${String(minutes).padStart(2, "0")}m`;
  if (minutes) return `${minutes}m${String(seconds % 60).padStart(2, "0")}s`;
  return `${seconds}s`;
}

/** 把路径拆成目录与文件名，列表里截断目录、保住文件名。 */
export function splitPath(path: string): { dir: string; base: string } {
  const index = path.lastIndexOf("/");
  return index < 0 ? { dir: "", base: path } : { dir: path.slice(0, index + 1), base: path.slice(index + 1) };
}

export function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
