import type { paths } from "./schema";

export type TaskListResponse = {
  items: TaskRow[];
  total: number;
  page: number;
  page_size: number;
};

export type StepState = "done" | "current" | "blocked" | "pending" | "failed";

export type Step = {
  number: number;
  key: string;
  name: string;
  state: StepState;
  detail: string;
  ok: boolean;
};

export type TaskRow = {
  id: string;
  task_root: string;
  task_name: string;
  source_kind: string;
  engineering_id?: string | null;
  project_id?: string | null;
  claim_id?: string | null;
  domain: string;
  sub_direction: string;
  task_type?: string | null;
  difficulty?: string | null;
  languages?: string | null;
  repro_level?: string | null;
  job_status: string;
  skill_status?: string | null;
  skill_version?: string | null;
  initial_sha?: string | null;
  artifact_sha?: string | null;
  session_id?: string | null;
  prompt_id?: string | null;
  prompt_sha256?: string | null;
  feedback_score?: number | null;
  ack_by?: string | null;
  review_acked_at?: string | null;
  exported_at?: string | null;
  created_at: string;
  updated_at: string;
  notes?: string | null;
  presence?: "present" | "missing";
  queue_position?: number | null;
  steps?: StoredStep[];
  run?: RunStatus;
};

export type StoredStep = {
  task_id: string;
  step_no: number;
  state: StepState;
  detail: string;
  updated_at: string;
};

export type TaskDetail = Omit<TaskRow, "steps"> & {
  complete: boolean;
  missing: string[];
  compatible: boolean;
  compatibility_error: string;
  state: Record<string, unknown>;
  steps: Step[];
  source: Record<string, unknown>;
};

export type SystemStatus = {
  docker_ok: boolean;
  source_online: boolean;
  offline: boolean;
  disk_free_gb: number;
  disk_low: boolean;
  running: number;
  queued: number;
  blocked: number;
  auth_blocked: number;
  skill_version: string;
};

export type PurgeItem = {
  task_id: string;
  task_name: string;
  job_status: string;
  paths: string[];
  size: number;
  blockers: string[];
  warnings: string[];
  removed?: boolean;
  moved_to?: string;
  release_error?: string;
  error?: string;
};

export type PurgeResult = {
  dry_run: boolean;
  permanent: boolean;
  trash: string;
  items: PurgeItem[];
  total_bytes: number;
};

export type AutopilotJob = {
  task_id: string;
  task_name: string;
  stage: string;
  stage_label: string;
  detail: string;
  attempts: Record<string, number>;
  log: string;
  active: boolean;
  agent_running: boolean;
  started_at: string;
  updated_at: string;
  finished_at: string;
  failure_kind: string;
  elapsed_seconds: number;
  stage_started_at: string;
  stages: AutopilotStage[];
  retry_stages: string[];
  retry_options: Array<{ stage: string; label: string; available: boolean; reason: string }>;
  audit?: AuditRow | null;
  desktop_running: boolean;
  desktop_marker: string;
  desktop_prompt_file: string;
  desktop_rollout: string;
  desktop_thread_id: string;
  desktop_started_at: string;
  desktop_last_progress_at: string;
};

export type AutopilotSnapshot = {
  enabled: boolean;
  agent_mode: "chatgpt" | "cli";
  chatgpt_available: boolean;
  paused: boolean;
  paused_by_breaker: boolean;
  breaker: {
    tripped: boolean;
    kind: string;
    since: string;
    consecutive_kind: string;
    consecutive_count: number;
    audit_verdicts: string[];
  };
  agent_command: string;
  agent_available: boolean;
  agents_running: number;
  active: number;
  parallel_authoring: number;
  parallel_delivering: number;
  stage_timeout_seconds: number;
  chatgpt_timeout_seconds: number;
  jobs: AutopilotJob[];
};

export type AutopilotStage = {
  id: number;
  task_id: string;
  stage: string;
  attempt: number;
  started_at: string;
  ended_at?: string | null;
  outcome?: string | null;
  failure_kind?: string | null;
  failure_detail?: string | null;
  usage_json?: Record<string, unknown> | null;
  log_path: string;
};

export type PreflightItem = {
  key: string;
  level: string;
  ok: boolean;
  message: string;
  fix: string;
};

export type PromptTemplate = {
  stage: string;
  name: string;
  path: string;
  modified_at: string;
  content: string;
  sha256: string;
  errors: string[];
};

export type AuditRow = {
  task_id: string;
  sampled: number | boolean;
  verdict?: "pass" | "reject" | null;
  reason?: string | null;
  disposition?: string | null;
  decided_at?: string | null;
  task_name?: string;
  task_type?: string;
  ack_by?: string;
};

export type AuditDetail = {
  audit: AuditRow;
  task: TaskRow;
  steps: Step[];
  state: Record<string, unknown>;
  prompt: PromptData;
  feedback: FeedbackResponse;
  timeline: TimelineResponse;
  diff: DiffResponse;
  recording: RecordingVerify;
  recent_feedback: Array<{ task_id: string; task_name: string; head: string }>;
  readonly: true;
};

export type BatchIssue = {
  code: string;
  task_id: string;
  task: string;
  message: string;
};

export type BatchCheck = {
  ok: boolean;
  task_ids: string[];
  blockers: BatchIssue[];
  warnings: BatchIssue[];
  unaudited_count: number;
  checked_at: string;
};

export type BatchRow = {
  id: string;
  created_at: string;
  xlsx_path: string;
  rows?: number;
  task_ids: string[];
  check: BatchCheck;
  unaudited_count: number;
  exists?: boolean;
  size?: number;
};

export type IntakeSnapshot = {
  plan: {
    id: string;
    project_id: string;
    filters: Record<string, unknown>;
    target: number;
    status: string;
    created_at: string;
    updated_at: string;
    task_ids: string[];
    failed_claims: Array<{ engineering_id: string; code?: string; message?: string }>;
    pause_reason?: string;
  } | null;
  counts: { target: number; completed: number; active: number; failed: number; claimed: number };
  max_in_hand: number;
  eta: string;
  attempts_left?: number;
};

export type TrashItem = {
  name: string;
  task_id: string;
  task_name: string;
  cleaned_at: string;
  expires_at?: string;
  size: number;
  remaining_days?: number | null;
  legacy?: boolean;
};

export type UsageRow = {
  project_id: string;
  project_name: string;
  engineering_id: string;
  engineering_name: string;
  used: number;
  claimed_unused: number;
  delivered: number;
  submitted: number;
  delivery_rate: number;
  last_used_at: string;
  last_outcome?: string;
  outcome?: string;
  last_failure_kind?: string;
  failure_kind?: string;
  task_id?: string;
  task_name?: string;
  claimed_at?: string;
  task_created_at?: string;
  delivered_at?: string;
  submitted_at?: string;
  purged_at?: string;
  task_exists?: boolean;
};

export type QueueSnapshot = {
  limit: number;
  used: number;
  slots: Array<{ task_id: string; task_name: string; phase: string; attempt: number; started_at: string }>;
  waiting: Array<{ position: number; task_id: string; task_name: string; pinned: boolean; enqueued_at: string }>;
};

export type RunStatus = {
  state: string;
  attempt_seq: number;
  attempt_total: number;
  phase: string;
  started_at: string;
  elapsed_s: number;
  queue_position?: number;
  log_tail: string;
};

export type EventRow = {
  id: number;
  at: string;
  level: string;
  actor: string;
  task_id?: string | null;
  action: string;
  message: string;
  details?: Record<string, unknown>;
};

export type ProjectRow = {
  id: string;
  name: string;
  description?: string;
  status?: string;
  start_at?: string;
  end_at?: string;
  engineering_count?: number;
  usage?: { used: number; delivered: number; submitted: number; claimed_unused: number; delivery_rate: number };
  [key: string]: unknown;
};

export type EngineeringRow = {
  id?: string;
  engineering_id?: string;
  name: string;
  languages?: Array<string | { name?: string; files?: number; percentage?: number }>;
  tech_stack?: Array<string | { name?: string; files?: number; percentage?: number }>;
  source_file_count?: number;
  version_number?: number;
  tags?: Array<string | { id: string; name: string }>;
  can_download?: boolean;
  claimed_by?: string | null;
  claimed_name?: string;
  claim_id?: string;
  assets?: Array<{ id?: string; url?: string; kind?: string }>;
  usage?: {
    used: number;
    delivered: number;
    submitted: number;
    claimed_unused: number;
    delivery_rate: number;
    last_outcome: string;
    last_failure_kind: string;
  };
  [key: string]: unknown;
};

export type StagingRow = {
  engineeringId: string;
  projectId: string;
  name: string;
  claimId: string;
  zipSha256: string;
  stagingPath: string;
  sourcePath: string;
  taskRoot?: string;
  downloadedAt: string;
  [key: string]: unknown;
};

export type PromptData = {
  draft: string;
  installed: string;
  installed_sha256: string;
  readonly: boolean;
  can_reopen: boolean;
  chars: number;
  limits: { preferred: number; hard: number };
};

export type PromptPreview = {
  ok: boolean;
  chars: number;
  errors: string[];
  warnings: string[];
};

export type PromptInstallResponse = {
  ok: boolean;
  errors: string[];
  warnings: string[];
  installed_sha256: string;
  stderr: string;
};

export type TimelineStep = {
  no: number;
  kind: string;
  tool: string;
  summary: string;
  status: "ok" | "failed";
  ts: string;
  body_ref: string;
};

export type TimelineResponse = {
  steps: TimelineStep[];
  counts: { total: number; failed: number };
};

export type TimelineBody = {
  input: Record<string, unknown>;
  output: string;
  is_error: boolean;
  tool_use_id: string;
};

export type DiffResponse = {
  files: Array<{ path: string; add: number; del: number; status: string }>;
  total: { add: number; del: number; files: number };
  short_stat: string;
};

export type FeedbackResponse = {
  text: string;
  chars: number;
  anchors: number;
  digits_ratio: number;
  coverage: { process: boolean; artifact: boolean };
  checked: { ok: boolean; score: number | null; errors: string[]; warnings: string[]; stale: boolean };
  review_ack: { acked?: boolean; valid?: boolean; ackedAt?: string; [key: string]: unknown };
};

export type CheckResponse = {
  ok: boolean;
  score: number | null;
  errors: string[];
  warnings: string[];
  stderr: string;
  exit_code: number;
};

export type RecordPlanResponse = {
  plan: Record<string, unknown>;
  exists: boolean;
  checked: { ok: boolean; errors?: string[]; warnings?: string[]; mode?: string };
  path: string;
};

export type WindowRow = {
  id: number;
  windowId: number;
  app: string;
  title: string;
  width: number;
  height: number;
  owner_pid: number;
};

export type RecordingStatus = {
  recording: boolean;
  pending?: boolean;
  task_id?: string;
  countdown_remaining?: number;
  elapsed_s: number;
  remaining_s: number;
  external?: boolean;
};

export type RecordingVerify = {
  ok: boolean;
  width: number;
  height: number;
  duration_s: number;
  capture_kind: string;
  cursor_ok: boolean;
  errors: string[];
};

export type ConfigResponse = {
  host: string;
  port: number;
  skill_root: string;
  skill_version: string;
  tasks_dir: string;
  source: {
    base_url: string;
    auth_mode: string;
    username: string;
    timeout_seconds: number;
    password: string;
    bearer_token: string;
  };
  execution: { max_containers: number; run_timeout_seconds: number; attempts: number; stale_after_seconds: number };
  storage: { disk_floor_gb: number; event_retention_days: number; skill_log_retention_days: number };
  recording: { max_seconds: number; chrome_url: string };
  autopilot: {
    enabled: boolean;
    agent_mode: "chatgpt" | "cli";
    agent_bin: string;
    agent_args: string[];
    chatgpt_folder_id: string;
    chatgpt_push_helper: string;
    chatgpt_timeout_seconds: number;
    restricted: boolean;
    stage_timeout_seconds: number;
    stage_attempts: number;
    run_attempts: number;
    parallel_authoring: number;
    parallel_delivering: number;
    stop_kills_containers: boolean;
    breaker: { same_kind_failures: number; audit_reject_rate: number; audit_reject_window: number };
  };
  audit: { mode: "all" | "sample" | "off"; sample_rate: number; stratify_by: string };
  intake: { max_in_hand: number; exclude_tags: string[] };
  trash: { retention_days: number };
  slim: { enabled: boolean; after_days: number; patterns: string[] };
  notify: { macos: boolean; webhook_url: string };
  solo2: {
    base_url: string;
    username: string;
    timeout_seconds: number;
    password: string;
  };
  skill_config: Record<string, unknown>;
  github_token: string;
};

export type ExportResult = {
  file: string;
  rows: number;
  skipped: Array<{ task_id: string; task: string; reason: string[] }>;
};

export type ExportHistoryRow = { name: string; path: string; size: number; created_at: string };

export type ConfigPatchRequest =
  paths["/api/system/config"]["patch"]["requestBody"]["content"]["application/json"];
