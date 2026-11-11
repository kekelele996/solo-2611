"""Request models shared by the API."""

from __future__ import annotations

from typing import Literal

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
    secrets: SecretConfigPatch | None = None


class AutopilotRequest(BaseModel):
    task_ids: list[str] = Field(default_factory=list, max_length=200)
