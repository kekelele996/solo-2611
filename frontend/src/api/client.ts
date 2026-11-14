import type {
  AuditDetail,
  AuditRow,
  AutopilotRecovery,
  AutopilotSnapshot,
  BatchCheck,
  BatchRow,
  CheckResponse,
  ConfigPatchRequest,
  ConfigResponse,
  CoreTraceResponse,
  DiffResponse,
  DispatchLimitsRequest,
  DispatchLogRow,
  DispatchSnapshot,
  DispatchUpcomingResponse,
  EngineeringRow,
  EventRow,
  ExportHistoryRow,
  ExportResult,
  FeedbackResponse,
  IntakeSnapshot,
  PreflightItem,
  PromptTemplate,
  PromptData,
  PromptInstallResponse,
  PromptLibraryImportResult,
  PromptLibraryResponse,
  PromptPreview,
  ProjectRow,
  PurgeResult,
  QueueSnapshot,
  RecordPlanResponse,
  RecordingStatus,
  RecordingVerify,
  RunStatus,
  SessionStats,
  SessionStatus,
  StagingRow,
  SystemStatus,
  TaskDetail,
  TaskListResponse,
  TimelineBody,
  TimelineResponse,
  TrashItem,
  UsageRow,
  WindowRow,
} from "./types";
import { APP_BUILD_ID } from "../utils/build";

type ApiEnvelope = { error?: { code?: string; message?: string; detail?: unknown } };

export class ApiError extends Error {
  status: number;
  code: string;
  detail: unknown;

  constructor(status: number, code: string, message: string, detail: unknown = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.detail = detail;
  }
}

let backendBuildId = "";

export function getBackendBuildId(): string {
  return backendBuildId;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  if (init?.body && !(init.body instanceof FormData)) {
    headers.set("Content-Type", "application/json");
  }
  const response = await fetch(path, { ...init, headers });
  if (!response.ok) {
    let payload: ApiEnvelope = {};
    try {
      payload = (await response.json()) as ApiEnvelope;
    } catch {
      payload = {};
    }
    const mismatch = backendBuildId && backendBuildId !== APP_BUILD_ID;
    throw new ApiError(
      response.status,
      payload.error?.code ?? `HTTP_${response.status}`,
      mismatch && (response.status === 404 || response.status === 405)
        ? "页面与后端版本不一致，需要重启后端。"
        : payload.error?.message ?? `${response.status} ${response.statusText}`,
      payload.error?.detail ?? null,
    );
  }
  if (response.status === 204) {
    return undefined as T;
  }
  return (await response.json()) as T;
}

function qs(params: Record<string, string | number | boolean | string[] | null | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === "") continue;
    if (Array.isArray(value)) {
      value.forEach((item) => search.append(key, item));
    } else {
      search.set(key, String(value));
    }
  }
  const encoded = search.toString();
  return encoded ? `?${encoded}` : "";
}

export const api = {
  health: () => request<{ ok: boolean; uptime_s: number; version: string; build_id: string }>("/api/health"),
  version: async () => {
    const data = await request<{
      app: string;
      git_sha: string;
      build_id: string;
      skill_version: string;
      skill_schema: string;
      image: string;
    }>("/api/version");
    backendBuildId = data.build_id || "legacy";
    return data;
  },
  systemStatus: () => request<SystemStatus>("/api/system/status"),
  reconcile: () => request<{ corrected: number }>("/api/system/reconcile", { method: "POST" }),
  config: () => request<ConfigResponse>("/api/system/config"),
  patchConfig: (payload: ConfigPatchRequest) =>
    request<ConfigResponse>("/api/system/config", {
      method: "PATCH",
      body: JSON.stringify(payload),
    }),
  testConfig: (target: "skill" | "source" | "docker" | "solo2" | "api") =>
    request<{ ok: boolean; message: string; detail: Record<string, unknown> }>("/api/system/config/test", {
      method: "POST",
      body: JSON.stringify({ target }),
    }),
  backup: () =>
    request<{ path: string; size: number }>("/api/system/backup", {
      method: "POST",
    }),
  diagnostics: () =>
    request<{ path: string; size: number }>("/api/system/diagnostics", {
      method: "POST",
    }),
  restart: (force = false) =>
    request<{ ok: boolean; restarting: boolean; build_id: string; hint: string }>("/api/system/restart", {
      method: "POST",
      body: JSON.stringify({ force }),
    }),
  prune: (dryRun = true, confirm = false) =>
    request<{ dry_run: boolean; items: Array<{ path: string; size: number }>; total_bytes: number }>("/api/system/prune", {
      method: "POST",
      body: JSON.stringify({ dry_run: dryRun, scopes: ["staging", "logs"], confirm }),
    }),
  logs: (tail = 200) => request<{ items: EventRow[] }>(`/api/system/logs${qs({ tail })}`),
  sourceLogin: (username: string, password: string) =>
    request<{ ok: boolean; user: Record<string, unknown> }>("/api/source/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    }),
  projects: (refresh = false) =>
    request<{ items: ProjectRow[]; total: number }>(`/api/source/projects${qs({ refresh })}`),
  engineering: (projectId: string, filters: Record<string, string | number | string[] | undefined>) =>
    request<{ items: EngineeringRow[]; total: number; page: number; page_size: number }>(
      `/api/source/projects/${encodeURIComponent(projectId)}/engineering${qs({ scope: "available", ...filters })}`,
    ),
  engineeringDetail: (projectId: string, engineeringId: string) =>
    request<EngineeringRow>(
      `/api/source/projects/${encodeURIComponent(projectId)}/engineering/${encodeURIComponent(engineeringId)}`,
    ),
  setEngineeringDisabled: (projectId: string, engineeringId: string, disabled: boolean, name = "") =>
    request<{ ok: boolean; engineering_id: string; disabled: boolean }>(
      `/api/source/projects/${encodeURIComponent(projectId)}/engineering/${encodeURIComponent(engineeringId)}/disabled`,
      { method: "PUT", body: JSON.stringify({ disabled, name }) },
    ),
  claim: (projectId: string, engineeringId: string) =>
    request<{ claim_id: string; staging_id: string; staging_path: string; sha256_verified: boolean }>("/api/claims", {
      method: "POST",
      body: JSON.stringify({ project_id: projectId, engineering_id: engineeringId }),
    }),
  releaseClaim: (claimId: string, force = false) =>
    request<{ ok: boolean }>(`/api/claims/${encodeURIComponent(claimId)}${qs({ force })}`, { method: "DELETE" }),
  claims: () => request<{ items: StagingRow[] }>("/api/claims"),
  staging: () => request<{ items: StagingRow[] }>("/api/staging"),
  createTask: (body: { staging_id: string; task_name?: string; domain?: string; sub_direction?: string }) =>
    request<{ task_id: string; task_root: string; initial_sha: string }>("/api/tasks", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  tasks: (filters: Record<string, string | number | undefined> = {}) =>
    request<TaskListResponse>(`/api/tasks${qs(filters)}`),
  task: (id: string) => request<TaskDetail>(`/api/tasks/${encodeURIComponent(id)}`),
  taskEvents: (id: string) => request<{ items: EventRow[] }>(`/api/tasks/${encodeURIComponent(id)}/events`),
  abortTask: (id: string, reason: string) =>
    request<{ ok: boolean; cleanup_required: boolean }>(`/api/tasks/${encodeURIComponent(id)}/abort`, {
      method: "POST",
      body: JSON.stringify({ reason }),
    }),
  purgeTasks: (taskIds: string[], options: { dryRun: boolean; permanent?: boolean }) =>
    request<PurgeResult>("/api/tasks/purge", {
      method: "POST",
      body: JSON.stringify({ task_ids: taskIds, dry_run: options.dryRun, permanent: options.permanent ?? false }),
    }),
  archiveTask: (id: string) =>
    request<{ ok: boolean; release_error: string }>(`/api/tasks/${encodeURIComponent(id)}/archive`, {
      method: "POST",
    }),
  prompt: (id: string) => request<PromptData>(`/api/tasks/${encodeURIComponent(id)}/prompt`),
  reviews: (id: string) =>
    request<{ difficulty: Record<string, unknown>; ra_renhua: Record<string, unknown> }>(
      `/api/tasks/${encodeURIComponent(id)}/reviews`,
    ),
  saveReview: (id: string, kind: "难度论证" | "ra-人话审核", data: Record<string, unknown>) =>
    request<Record<string, unknown>>(`/api/tasks/${encodeURIComponent(id)}/reviews/${encodeURIComponent(kind)}`, {
      method: "PUT",
      body: JSON.stringify(data),
    }),
  savePrompt: (id: string, text: string) =>
    request<PromptPreview>(`/api/tasks/${encodeURIComponent(id)}/prompt/draft`, {
      method: "PUT",
      body: JSON.stringify({ text }),
    }),
  reopenPrompt: (id: string, reason: string) =>
    request<{ ok: boolean; reason: string }>(`/api/tasks/${encodeURIComponent(id)}/prompt/reopen`, {
      method: "POST",
      body: JSON.stringify({ reason }),
    }),
  previewPrompt: (id: string, text: string) =>
    request<PromptPreview>(`/api/tasks/${encodeURIComponent(id)}/prompt/preview`, {
      method: "POST",
      body: JSON.stringify({ text }),
    }),
  promptLibrary: (projectId: string) =>
    request<PromptLibraryResponse>(
      `/api/source/prompts${qs({ project_id: projectId })}`,
    ),
  importPromptLibrary: (
    projectId: string,
    items: Array<Record<string, unknown>>,
    sourceName = "",
  ) =>
    request<PromptLibraryImportResult>("/api/source/prompts/import", {
      method: "POST",
      body: JSON.stringify({
        project_id: projectId,
        source_name: sourceName,
        items,
      }),
    }),
  exportPromptLibrary: async (projectId: string) => {
    const response = await fetch(
      `/api/source/prompts/export${qs({ project_id: projectId })}`,
    );
    if (!response.ok) {
      throw new ApiError(
        response.status,
        "PROMPT_LIBRARY_EXPORT_FAILED",
        (await response.text()) || "提示词库导出失败",
      );
    }
    return response.text();
  },
  installPrompt: (
    id: string,
    body: {
      task_type: string;
      difficulty: "困难" | "地狱";
      languages: string;
      repro_level: string | null;
      domain: string;
      sub_direction: string;
      allow_over_170: boolean;
    },
  ) =>
    request<PromptInstallResponse>(`/api/tasks/${encodeURIComponent(id)}/prompt/install`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  repo: (id: string) =>
    request<Record<string, unknown>>(`/api/tasks/${encodeURIComponent(id)}/repo`, { method: "POST" }),
  snapshots: (id: string) =>
    request<{
      initial: { sha: string; permalink: string };
      artifact: { sha: string; permalink: string; short_stat: string };
      ancestor_ok: boolean;
    }>(`/api/tasks/${encodeURIComponent(id)}/snapshots`),
  enqueue: (id: string, pinned = false) =>
    request<{ state: string; queue_position: number | null }>(`/api/tasks/${encodeURIComponent(id)}/run`, {
      method: "POST",
      body: JSON.stringify({ pinned }),
    }),
  runStatus: (id: string) => request<RunStatus>(`/api/tasks/${encodeURIComponent(id)}/run`),
  session: (id: string) => request<SessionStatus>(`/api/tasks/${encodeURIComponent(id)}/run/session`),
  resumeSession: (id: string) =>
    request<{ ok: boolean; status: SessionStatus }>(`/api/tasks/${encodeURIComponent(id)}/run/resume`, {
      method: "POST",
    }),
  continueSession: (id: string, note = "工作台人工继续") =>
    request<{ ok: boolean; sent: boolean; status: SessionStatus }>(
      `/api/tasks/${encodeURIComponent(id)}/run/continue`,
      { method: "POST", body: JSON.stringify({ note }) },
    ),
  abortSession: (id: string, reason: string) =>
    request<{ ok: boolean; job_status: string }>(`/api/tasks/${encodeURIComponent(id)}/run/abort`, {
      method: "POST",
      body: JSON.stringify({ reason }),
    }),
  sessionStats: () => request<SessionStats>("/api/dispatch/session-stats"),
  stopRun: (id: string, force = false) =>
    request<{ state: string }>(`/api/tasks/${encodeURIComponent(id)}/run/stop`, {
      method: "POST",
      body: JSON.stringify({ force }),
    }),
  runLog: async (id: string, lines = 200) => {
    const response = await fetch(`/api/tasks/${encodeURIComponent(id)}/run/log${qs({ lines })}`);
    if (!response.ok) throw new ApiError(response.status, "RUN_LOG_FAILED", await response.text());
    return response.text();
  },
  interactive: (id: string) =>
    request<{ command: string; container_hint: string }>(`/api/tasks/${encodeURIComponent(id)}/run/interactive`, {
      method: "POST",
    }),
  collect: (id: string, container: string | null) =>
    request<{ ok: boolean; errors: string[]; stderr: string }>(`/api/tasks/${encodeURIComponent(id)}/run/collect`, {
      method: "POST",
      body: JSON.stringify({ container }),
    }),
  reset: (id: string, confirm: string) =>
    request<{ ok: boolean }>(`/api/tasks/${encodeURIComponent(id)}/reset`, {
      method: "POST",
      body: JSON.stringify({ confirm }),
    }),
  queue: () => request<QueueSnapshot>("/api/queue"),
  timeline: (id: string) => request<TimelineResponse>(`/api/tasks/${encodeURIComponent(id)}/timeline`),
  timelineBody: (id: string, no: number) =>
    request<TimelineBody>(`/api/tasks/${encodeURIComponent(id)}/timeline/steps/${no}`),
  coreTrace: (id: string) =>
    request<CoreTraceResponse>(`/api/tasks/${encodeURIComponent(id)}/core-trace`),
  diff: (id: string) => request<DiffResponse>(`/api/tasks/${encodeURIComponent(id)}/diff`),
  diffFile: async (id: string, path: string) => {
    const response = await fetch(`/api/tasks/${encodeURIComponent(id)}/diff/file${qs({ path })}`);
    if (!response.ok) throw new ApiError(response.status, "DIFF_FAILED", await response.text());
    return response.text();
  },
  verify: (id: string) =>
    request<{ structure: Record<string, unknown>; runtime: Record<string, unknown> }>(
      `/api/tasks/${encodeURIComponent(id)}/verify`,
    ),
  runVerify: (id: string, cmd: string) =>
    request<Record<string, unknown>>(`/api/tasks/${encodeURIComponent(id)}/verify`, {
      method: "POST",
      body: JSON.stringify({ cmd }),
    }),
  artifacts: (id: string) =>
    request<{ items: Array<{ kind: string; path: string; size: number; sha256: string }> }>(
      `/api/tasks/${encodeURIComponent(id)}/artifacts`,
    ),
  reveal: (id: string, kind: string) =>
    request<{ ok: boolean; path: string }>(`/api/tasks/${encodeURIComponent(id)}/reveal`, {
      method: "POST",
      body: JSON.stringify({ kind }),
    }),
  windows: () => request<{ windows: WindowRow[] }>("/api/recording/windows"),
  recordPlan: (id: string) => request<RecordPlanResponse>(`/api/tasks/${encodeURIComponent(id)}/record/plan`),
  saveRecordPlan: (id: string, plan: Record<string, unknown>) =>
    request<RecordPlanResponse>(`/api/tasks/${encodeURIComponent(id)}/record/plan`, {
      method: "PUT",
      body: JSON.stringify(plan),
    }),
  generateRecordPlan: (id: string, mode: "terminal" | "web", force = false) =>
    request<RecordPlanResponse>(`/api/tasks/${encodeURIComponent(id)}/record/plan/generate`, {
      method: "POST",
      body: JSON.stringify({ mode, force }),
    }),
  recordingStatus: () => request<RecordingStatus>("/api/recording/status"),
  startRecording: (id: string, windowId: number) =>
    request<{ recording: boolean; pending: boolean; countdown: number }>(
      `/api/tasks/${encodeURIComponent(id)}/record/start`,
      { method: "POST", body: JSON.stringify({ window_id: windowId }) },
    ),
  stopRecording: (_id: string) =>
    request<{ ok: boolean; recording: Record<string, unknown> }>('/api/recording/stop', {
      method: "POST",
    }),
  verifyRecording: (id: string) => request<RecordingVerify>(`/api/tasks/${encodeURIComponent(id)}/record/verify`),
  deleteRecording: (id: string) =>
    request<{ ok: boolean; removed: string[] }>(`/api/tasks/${encodeURIComponent(id)}/record/delete`, {
      method: "POST",
      body: JSON.stringify({ confirm: true }),
    }),
  autopilot: () => request<AutopilotSnapshot>("/api/autopilot"),
  dispatch: () => request<DispatchSnapshot>("/api/dispatch"),
  dispatchState: (desired: "running" | "paused" | "draining") =>
    request<DispatchSnapshot>("/api/dispatch/state", {
      method: "POST",
      body: JSON.stringify({ desired }),
    }),
  dispatchInterrupt: (taskIds: string[] = [], killContainers = true) =>
    request<Record<string, unknown>>("/api/dispatch/interrupt", {
      method: "POST",
      body: JSON.stringify({ task_ids: taskIds, kill_containers: killContainers }),
    }),
  dispatchLimits: (payload: DispatchLimitsRequest) =>
    request<{
      snapshot: DispatchSnapshot;
      changed: Record<string, unknown[]>;
      sync: { requested: number; actual: number; synced: boolean; ceiling: number };
    }>(
      "/api/dispatch/limits",
      { method: "PATCH", body: JSON.stringify(payload) },
    ),
  dispatchExplain: (taskId = "") =>
    request<{ task_id?: string; sentence: string; decision: Record<string, unknown> | null }>(
      `/api/dispatch/explain${qs({ task_id: taskId })}`,
    ),
  dispatchUpcoming: (limit = 20) =>
    request<DispatchUpcomingResponse>(`/api/dispatch/upcoming${qs({ limit })}`),
  dispatchThroughput: (hours = 24) =>
    request<DispatchSnapshot["throughput"] & { hours: number }>(`/api/dispatch/throughput${qs({ hours })}`),
  dispatchCapacity: (hours = 24) =>
    request<{ items: Array<Record<string, unknown>> }>(`/api/dispatch/capacity${qs({ hours })}`),
  dispatchLog: (params: {
    afterSeq?: number;
    family?: string;
    level?: string;
    taskId?: string;
    limit?: number;
  } = {}) =>
    request<{ items: DispatchLogRow[]; after_seq: number }>(
      `/api/dispatch/log${qs({
        after_seq: params.afterSeq,
        family: params.family,
        level: params.level,
        task_id: params.taskId,
        limit: params.limit,
      })}`,
    ),
  dispatchHolds: () =>
    request<{ holds: DispatchSnapshot["holds"]; history: DispatchSnapshot["hold_history"] }>("/api/dispatch/holds"),
  dispatchProbeHold: (id: string) =>
    request<{ source: string; ok: boolean; message: string; detail: Record<string, unknown> }>(
      `/api/dispatch/holds/${encodeURIComponent(id)}/probe`,
      { method: "POST" },
    ),
  dispatchProbe: (source: string) =>
    request<{ source: string; ok: boolean; message: string; detail: Record<string, unknown> }>(
      `/api/dispatch/probes/${encodeURIComponent(source)}`,
      { method: "POST" },
    ),
  dispatchRecover: (sinceMinutes = 120) =>
    request<{ requeued: number; preserved: number; task_ids: string[] }>("/api/dispatch/recover", {
      method: "POST",
      body: JSON.stringify({ since_minutes: sinceMinutes }),
    }),
  dispatchQueue: (taskIds: string[], front = false) =>
    request<DispatchSnapshot & { added: string[]; skipped: Array<{ task_id: string; reason: string }> }>(
      "/api/dispatch/queue",
      { method: "POST", body: JSON.stringify({ task_ids: taskIds, front }) },
    ),
  dispatchQueueAction: (
    taskId: string,
    action: "pin" | "move",
    direction?: "up" | "down" | "top",
  ) =>
    request<{ ok: boolean; snapshot?: DispatchSnapshot }>(
      `/api/dispatch/queue/${encodeURIComponent(taskId)}/${action}`,
      {
        method: "POST",
        body: direction ? JSON.stringify({ direction }) : undefined,
      },
    ),
  dispatchClearFinished: () =>
    request<{ removed: number; snapshot: DispatchSnapshot }>("/api/dispatch/queue/clear-finished", {
      method: "POST",
    }),
  dispatchGuard: () =>
    request<{ mode: string; items: Array<Record<string, unknown>> }>("/api/dispatch/guard"),
  dispatchGuardAction: (hitId: string, action: "apply" | "dismiss") =>
    request<Record<string, unknown>>(`/api/dispatch/guard/${encodeURIComponent(hitId)}/${action}`, {
      method: "POST",
    }),
  autopilotStart: (taskIds: string[]) =>
    request<AutopilotSnapshot & AutopilotRecovery>("/api/autopilot/start", {
      method: "POST",
      body: JSON.stringify({ task_ids: taskIds }),
    }),
  autopilotStop: (taskIds: string[]) =>
    request<AutopilotSnapshot & { stopped: string[] }>("/api/autopilot/stop", {
      method: "POST",
      body: JSON.stringify({ task_ids: taskIds }),
    }),
  autopilotPreflight: () => request<{ ok: boolean; items: PreflightItem[] }>("/api/autopilot/preflight", { method: "POST" }),
  autopilotPause: () => request<AutopilotSnapshot>("/api/autopilot/pause", { method: "POST" }),
  autopilotResume: () => request<AutopilotSnapshot & AutopilotRecovery>("/api/autopilot/resume", { method: "POST" }),
  autopilotRetry: (id: string, fromStage = "") =>
    request<Record<string, unknown>>(`/api/autopilot/${encodeURIComponent(id)}/retry`, {
      method: "POST",
      body: JSON.stringify({ from_stage: fromStage }),
    }),
  autopilotStages: (id: string) =>
    request<{ items: Array<Record<string, unknown>> }>(`/api/autopilot/${encodeURIComponent(id)}/stages`),
  autopilotPrompts: () => request<{ items: PromptTemplate[]; readonly: boolean }>("/api/autopilot/prompts"),
  autopilotLog: (id: string, stage = "") =>
    request<{ task_id: string; stage: string; file: string; lines: Array<{ kind: string; text: string }> }>(
      `/api/autopilot/${encodeURIComponent(id)}/log${qs({ stage })}`,
    ),
  openChatGPT: (id: string) =>
    request<{ ok: boolean; thread_id: string; target: string }>(
      `/api/autopilot/${encodeURIComponent(id)}/chatgpt/open`,
      { method: "POST" },
    ),
  recordTerminal: (id: string) =>
    request<{ ok: boolean; window_id: number; title: string }>(
      `/api/tasks/${encodeURIComponent(id)}/record/terminal`,
      { method: "POST" },
    ),
  chromeStart: (id: string, url: string) =>
    request<Record<string, unknown>>(`/api/tasks/${encodeURIComponent(id)}/record/chrome-start`, {
      method: "POST",
      body: JSON.stringify({ url }),
    }),
  chromeStop: (id: string) =>
    request<Record<string, unknown>>(`/api/tasks/${encodeURIComponent(id)}/record/chrome-stop`, { method: "POST" }),
  initFeedback: (id: string) =>
    request<{ ok: boolean; path: string }>(`/api/tasks/${encodeURIComponent(id)}/feedback/init`, { method: "POST" }),
  feedback: (id: string) => request<FeedbackResponse>(`/api/tasks/${encodeURIComponent(id)}/feedback`),
  saveFeedback: (id: string, text: string) =>
    request<FeedbackResponse>(`/api/tasks/${encodeURIComponent(id)}/feedback`, {
      method: "PUT",
      body: JSON.stringify({ text }),
    }),
  checkFeedback: (id: string) =>
    request<CheckResponse>(`/api/tasks/${encodeURIComponent(id)}/feedback/check`, { method: "POST" }),
  ackFeedback: (id: string) =>
    request<Record<string, unknown>>(`/api/tasks/${encodeURIComponent(id)}/feedback/ack`, {
      method: "POST",
      body: JSON.stringify({ acked: true }),
    }),
  export: (body: { task_ids?: string[]; date?: string }) =>
    request<ExportResult>("/api/export", { method: "POST", body: JSON.stringify(body) }),
  exports: () => request<{ items: ExportHistoryRow[] }>("/api/exports"),
  exportPreview: (name: string) =>
    request<{ headers: Array<string | number | null>; rows: Array<Array<string | number | boolean | null>> }>(
      `/api/exports/${encodeURIComponent(name)}/preview`,
    ),
  revealExport: (name: string) =>
    request<{ ok: boolean; path: string }>(`/api/exports/${encodeURIComponent(name)}/reveal`, {
      method: "POST",
    }),
  stats: (from: string, to: string) =>
    request<{
      days: Array<{ date: string; created: number; exported: number; failed: number }>;
      summary: { created: number; exported: number; average_minutes: number };
      failures: Array<{ action: string; message: string; count: number }>;
    }>(
      `/api/stats${qs({ from, to })}`,
    ),
  audits: (verdict = "pending") => request<{ items: AuditRow[]; total: number }>(`/api/audits${qs({ verdict })}`),
  audit: (id: string) => request<AuditDetail>(`/api/audits/${encodeURIComponent(id)}`),
  decideAudit: (
    id: string,
    body: { verdict: "pass" | "reject"; reason?: string; disposition?: "redo_feedback" | "manual" | "void" },
  ) =>
    request<AuditRow>(`/api/audits/${encodeURIComponent(id)}`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  quality: (from = "", to = "") =>
    request<{
      audit: { sampled: number; decided: number; passed: number; rejected: number; pass_rate: number };
      scores: Array<{ score: string; count: number }>;
      feedback_lengths: { min: number; max: number; average: number; samples: number };
      failures: Array<{ kind: string; count: number }>;
      acknowledgement: Record<string, number>;
      warning: string;
      readonly: true;
    }>(`/api/quality/summary${qs({ from, to })}`),
  batchCheck: (taskIds: string[], includeExported = false) =>
    request<BatchCheck>("/api/batches/check", {
      method: "POST",
      body: JSON.stringify({ task_ids: taskIds, include_exported: includeExported }),
    }),
  createBatch: (taskIds: string[], includeExported = false, acknowledgeWarnings = false) =>
    request<BatchRow>("/api/batches", {
      method: "POST",
      body: JSON.stringify({
        task_ids: taskIds,
        include_exported: includeExported,
        acknowledge_warnings: acknowledgeWarnings,
      }),
    }),
  batches: () => request<{ items: BatchRow[]; total: number }>("/api/batches"),
  batch: (id: string) => request<BatchRow & { manifest: string }>(`/api/batches/${encodeURIComponent(id)}`),
  bundleBatch: (id: string) =>
    request<{ batch_id: string; bundle: string; files: string[]; note: string }>(
      `/api/batches/${encodeURIComponent(id)}/bundle`,
      { method: "POST" },
    ),
  privacyRecheck: (taskIds: string[]) =>
    request<{
      items: Array<{ task_id: string; status: string; ok: boolean; message: string }>;
      passed: number;
      unchecked: number;
      failed: number;
    }>("/api/recordings/privacy-recheck", {
      method: "POST",
      body: JSON.stringify({ task_ids: taskIds }),
    }),
  intake: () => request<IntakeSnapshot>("/api/intake"),
  createIntake: (body: {
    project_id: string;
    mode?: "target" | "continuous";
    target?: number;
    daily_cap?: number;
    filters?: Record<string, unknown>;
    exclude_delivered?: boolean;
    max_uses?: number;
  }) =>
    request<IntakeSnapshot>("/api/intake", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  pauseIntake: () => request<IntakeSnapshot>("/api/intake/pause", { method: "POST" }),
  resumeIntake: () => request<IntakeSnapshot>("/api/intake/resume", { method: "POST" }),
  stopIntake: (releaseUnstarted = false) =>
    request<IntakeSnapshot>("/api/intake/stop", {
      method: "POST",
      body: JSON.stringify({ release_unstarted: releaseUnstarted }),
    }),
  trash: () =>
    request<{ items: TrashItem[]; total: number; total_bytes: number; retention_days: number }>("/api/trash"),
  restoreTrash: (name: string) =>
    request<{ ok: boolean; restored: string[]; task_id: string }>(`/api/trash/${encodeURIComponent(name)}/restore`, {
      method: "POST",
    }),
  deleteTrash: (name: string) =>
    request<{ ok: boolean }>(`/api/trash/${encodeURIComponent(name)}`, { method: "DELETE" }),
  emptyTrash: () => request<{ ok: boolean; removed: string[] }>("/api/trash", { method: "DELETE" }),
  forgetTask: (id: string) =>
    request<{ ok: boolean }>(`/api/tasks/${encodeURIComponent(id)}/forget`, { method: "POST" }),
  usageProjects: (from = "", to = "") =>
    request<{ items: UsageRow[]; total: number; first_recorded_at: string }>(`/api/usage/projects${qs({ from, to })}`),
  usageEngineering: (projectId: string, from = "", to = "") =>
    request<{ items: UsageRow[]; total: number }>(
      `/api/usage/projects/${encodeURIComponent(projectId)}/engineering${qs({ from, to })}`,
    ),
  usageRecords: (engineeringId: string) =>
    request<{ items: UsageRow[]; total: number }>(`/api/usage/engineering/${encodeURIComponent(engineeringId)}`),
};
