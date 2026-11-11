"""Request models shared by the API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class SourceLoginRequest(BaseModel):
    username: str = ""
    password: str = ""


class ClaimRequest(BaseModel):
    project_id: str
    engineering_id: str


class CreateTaskRequest(BaseModel):
    staging_id: str
    task_name: str | None = None
    domain: str | None = None
    sub_direction: str | None = None


class AbortRequest(BaseModel):
    reason: str


class PromptDraftRequest(BaseModel):
    text: str


class PromptInstallRequest(BaseModel):
    task_type: str
    difficulty: Literal["困难", "地狱"]
    languages: str
    repro_level: str | None = None
    domain: str = "数据工程"
    sub_direction: str = "其他"
    allow_over_170: bool = False


class ReopenRequest(BaseModel):
    reason: str


class RunRequest(BaseModel):
    pinned: bool = False


class RunStopRequest(BaseModel):
    force: bool = False


class CollectRequest(BaseModel):
    container: str | None = None


class ResetRequest(BaseModel):
    confirm: str = ""


class VerifyRequest(BaseModel):
    cmd: str


class WindowStartRequest(BaseModel):
    window_id: int
    plan: str | None = None


class RecordPlanGenerateRequest(BaseModel):
    mode: Literal["terminal", "web"]
    force: bool = False


class RecordDeleteRequest(BaseModel):
    confirm: bool = False


class ChromeStartRequest(BaseModel):
    url: str


class FeedbackSaveRequest(BaseModel):
    text: str


class FeedbackAckRequest(BaseModel):
    acked: bool


class ExportRequest(BaseModel):
    task_ids: list[str] | None = None
    date: str | None = None


class PruneRequest(BaseModel):
    dry_run: bool = True
    scopes: list[str] = Field(default_factory=lambda: ["staging", "logs"])
    confirm: bool = False


class PurgeRequest(BaseModel):
    task_ids: list[str] = Field(min_length=1, max_length=200)
    dry_run: bool = True
    permanent: bool = False


class RevealRequest(BaseModel):
    kind: str


class ConfigTestRequest(BaseModel):
    target: Literal["skill", "source", "docker"]


class SourceConfigPatch(BaseModel):
    base_url: str | None = None
    auth_mode: Literal["cookie"] | None = None
    username: str | None = None
    timeout_seconds: float | None = None


class ExecutionConfigPatch(BaseModel):
    max_containers: int | None = None
    run_timeout_seconds: int | None = None
    attempts: int | None = None
    stale_after_seconds: int | None = None


class StorageConfigPatch(BaseModel):
    disk_floor_gb: float | None = None
    event_retention_days: int | None = None
    skill_log_retention_days: int | None = None


class RecordingConfigPatch(BaseModel):
    max_seconds: int | None = None
    chrome_url: str | None = None


class BreakerConfigPatch(BaseModel):
    same_kind_failures: int | None = None
    audit_reject_rate: float | None = None
    audit_reject_window: int | None = None


class AutopilotConfigPatch(BaseModel):
    enabled: bool | None = None
    agent_mode: Literal["chatgpt", "cli"] | None = None
    agent_bin: str | None = None
    agent_args: list[str] | None = None
    chatgpt_folder_id: str | None = None
    chatgpt_push_helper: str | None = None
    chatgpt_timeout_seconds: int | None = None
    restricted: bool | None = None
    stage_timeout_seconds: int | None = None
    stage_attempts: int | None = None
    run_attempts: int | None = None
    parallel_authoring: int | None = None
    parallel_delivering: int | None = None
    stop_kills_containers: bool | None = None
    breaker: BreakerConfigPatch | None = None


class AuditConfigPatch(BaseModel):
    mode: Literal["all", "sample", "off"] | None = None
    sample_rate: float | None = None
    stratify_by: str | None = None


class IntakeConfigPatch(BaseModel):
    max_in_hand: int | None = None
    exclude_tags: list[str] | None = None


class TrashConfigPatch(BaseModel):
    retention_days: int | None = None


class SlimConfigPatch(BaseModel):
    enabled: bool | None = None
    after_days: int | None = None
    patterns: list[str] | None = None


class NotifyConfigPatch(BaseModel):
    macos: bool | None = None
    webhook_url: str | None = None


class SecretConfigPatch(BaseModel):
    source_password: str | None = None
    source_bearer: str | None = None


class ConfigPatchRequest(BaseModel):
    host: Literal["127.0.0.1"] | None = None
    port: int | None = None
    skill_root: str | None = None
    tasks_dir: str | None = None
    source: SourceConfigPatch | None = None
    execution: ExecutionConfigPatch | None = None
    storage: StorageConfigPatch | None = None
    recording: RecordingConfigPatch | None = None
    autopilot: AutopilotConfigPatch | None = None
    audit: AuditConfigPatch | None = None
    intake: IntakeConfigPatch | None = None
    trash: TrashConfigPatch | None = None
    slim: SlimConfigPatch | None = None
    notify: NotifyConfigPatch | None = None
    secrets: SecretConfigPatch | None = None


class AutopilotRequest(BaseModel):
    task_ids: list[str] = Field(default_factory=list, max_length=200)
    kill_containers: bool | None = None


class AutopilotRetryRequest(BaseModel):
    from_stage: str = ""


class IntakeCreateRequest(BaseModel):
    project_id: str
    target: int = Field(ge=1, le=500)
    filters: dict[str, Any] = Field(default_factory=dict)
    exclude_delivered: bool = True
    max_uses: int = Field(default=0, ge=0, le=100)


class IntakeStopRequest(BaseModel):
    release_unstarted: bool = False


class AuditDecisionRequest(BaseModel):
    verdict: Literal["pass", "reject"]
    reason: str = ""
    disposition: Literal["redo_feedback", "manual", "void"] | None = None


class BatchCheckRequest(BaseModel):
    task_ids: list[str] = Field(min_length=1, max_length=500)
    include_exported: bool = False


class BatchCreateRequest(BatchCheckRequest):
    acknowledge_warnings: bool = False


class PrivacyRecheckRequest(BaseModel):
    task_ids: list[str] = Field(min_length=1, max_length=500)


class RestartRequest(BaseModel):
    force: bool = False
