"""Filesystem layout and editable application settings."""

from __future__ import annotations

import json
import logging
import os
import tomllib
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LOGGER = logging.getLogger(__name__)


def _default_tasks_dir() -> Path:
    skill_config = Path.home() / ".codex" / "solo-2611" / "config.json"
    try:
        data = json.loads(skill_config.read_text(encoding="utf-8"))
        value = data.get("tasksDir")
        if value:
            return Path(value).expanduser()
    except (OSError, json.JSONDecodeError):
        pass
    return Path.home() / "claude-runs" / "solo-2611"


def detect_skill_path() -> Path:
    candidates = [
        Path.home() / ".codex" / "skills" / "solo-2611",
        Path.home() / ".claude" / "skills" / "solo-2611",
    ]
    for candidate in candidates:
        if (candidate / "scripts" / "solo2611.py").is_file():
            return candidate
    return candidates[0]


class SourceSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    base_url: str = "http://localhost:8088"
    auth_mode: Literal["cookie"] = "cookie"
    username: str = ""
    timeout_seconds: float = Field(default=20.0, ge=1.0, le=300.0)

    @field_validator("base_url")
    @classmethod
    def _base_url(cls, value: str) -> str:
        return value.rstrip("/")


class ExecutionSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    max_containers: int = Field(default=4, ge=1, le=8)
    run_timeout_seconds: int = Field(default=7200, ge=60)
    attempts: int = Field(default=3, ge=1, le=10)
    stale_after_seconds: int = Field(default=90 * 60, ge=60)


class StorageSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    disk_floor_gb: float = Field(default=30.0, ge=0)
    event_retention_days: int = Field(default=30, ge=1)
    skill_log_retention_days: int = Field(default=14, ge=1)


class RecordingSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    max_seconds: int = Field(default=90, ge=1, le=90)
    chrome_url: str = "http://127.0.0.1:3000"


class SubmissionSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    auto_submit: bool = False
    poll_interval_seconds: int = Field(default=300, ge=10, le=3600)
    poll_timeout_seconds: int = Field(default=86400, ge=60, le=604800)


class BreakerSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    same_kind_failures: int = Field(default=3, ge=1, le=50)
    audit_reject_rate: float = Field(default=0.2, ge=0, le=1)
    audit_reject_window: int = Field(default=10, ge=1, le=100)


class AutopilotSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    agent_mode: Literal["chatgpt", "cli"] = "chatgpt"
    agent_bin: str = ""
    agent_args: list[str] = Field(default_factory=list)
    chatgpt_folder_id: str = ""
    chatgpt_push_helper: str = ""
    chatgpt_timeout_seconds: int = Field(default=12 * 60 * 60, ge=300)
    restricted: bool = False
    stage_timeout_seconds: int = Field(default=3600, ge=60)
    stage_attempts: int = Field(default=2, ge=1, le=10)
    run_attempts: int = Field(default=2, ge=1, le=10)
    parallel_authoring: int = Field(default=2, ge=1, le=8)
    parallel_delivering: int = Field(default=1, ge=1, le=8)
    stop_kills_containers: bool = True
    breaker: BreakerSettings = Field(default_factory=BreakerSettings)


class AuditSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    mode: Literal["all", "sample", "off"] = "sample"
    sample_rate: float = Field(default=0.3, ge=0, le=1)
    stratify_by: str = "task_type"


class IntakeSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    max_in_hand: int = Field(default=0, ge=0, le=100)
    exclude_tags: list[str] = Field(default_factory=list)


class TrashSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    retention_days: int = Field(default=7, ge=0, le=3650)


class SlimSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    after_days: int = Field(default=3, ge=0, le=365)
    patterns: list[str] = Field(
        default_factory=lambda: ["node_modules", ".venv", "dist", "build", "target", "__pycache__"]
    )


class NotifySettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    macos: bool = True
    webhook_url: str = ""


class Solo2Settings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    base_url: str = "https://solo2.jzxhnh.com/app"
    username: str = ""
    timeout_seconds: float = Field(default=30.0, ge=1.0, le=300.0)

    @field_validator("base_url")
    @classmethod
    def _base_url(cls, value: str) -> str:
        return value.strip().rstrip("/")


class DispatchElasticSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    ceiling: int = Field(default=6, ge=1, le=8)
    window_seconds: int = Field(default=300, ge=60, le=3600)
    stall_percent: float = Field(default=3.0, ge=0, le=100)
    severe_attempt: int = Field(default=6, ge=1, le=100)
    step_up_seconds: int = Field(default=300, ge=30, le=3600)
    cooldown_seconds: int = Field(default=600, ge=60, le=7200)
    stable_seconds: int = Field(default=1800, ge=300, le=14400)


class DispatchRetrySettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    max_total_attempts: int = Field(default=8, ge=1, le=100)
    backoff_seconds: list[int] = Field(default_factory=lambda: [60, 180, 600])
    jitter: float = Field(default=0.2, ge=0, le=1)
    unknown_retries: int = Field(default=1, ge=0, le=10)
    engineering_cooldown_failures: int = Field(default=2, ge=1, le=20)
    engineering_cooldown_hours: int = Field(default=72, ge=1, le=720)

    @field_validator("backoff_seconds")
    @classmethod
    def _backoff(cls, value: list[int]) -> list[int]:
        if not value:
            raise ValueError("退避序列不能为空")
        if any(item < 1 for item in value):
            raise ValueError("退避时间必须大于 0")
        return value


class DispatchGuardSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    mode: Literal["off", "observe", "enforce"] = "observe"
    run_overtime_factor: float = Field(default=1.25, ge=1.0, le=5.0)
    no_progress_minutes: int = Field(default=30, ge=5, le=1440)
    budget_authoring_minutes: int = Field(default=90, ge=5, le=1440)
    budget_delivering_minutes: int = Field(default=120, ge=5, le=1440)
    budget_recording_minutes: int = Field(default=20, ge=1, le=1440)
    budget_exporting_minutes: int = Field(default=15, ge=1, le=1440)
    zombie_grace_minutes: int = Field(default=5, ge=1, le=1440)
    orphan_container_minutes: int = Field(default=30, ge=1, le=10080)
    leaked_process_minutes: int = Field(default=60, ge=1, le=10080)
    claim_leak_minutes: int = Field(default=30, ge=1, le=1440)
    attempt_inflation: int = Field(default=50, ge=10, le=1000)


class DispatchLLMGuardSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    probe_seconds: int = Field(default=1800, ge=30, le=86400)
    retry_seconds: int = Field(default=30, ge=5, le=3600)
    paused_probe_seconds: int = Field(default=300, ge=10, le=3600)
    fail_threshold: int = Field(default=2, ge=1, le=10)
    timeout_seconds: int = Field(default=30, ge=1, le=300)


class DispatchDiskSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    floor_gb: float = Field(default=30.0, ge=0)
    resume_factor: float = Field(default=1.2, ge=1.0, le=5.0)
    emergency_gb: float = Field(default=10.0, ge=0)

    @model_validator(mode="after")
    def _thresholds(self) -> DispatchDiskSettings:
        if self.emergency_gb > self.floor_gb:
            raise ValueError("紧急磁盘阈值不能高于正常下限")
        return self


class DispatchRefillSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    mode: Literal["target", "continuous"] = "target"
    target: int = Field(default=0, ge=0, le=10000)
    daily_claim_cap: int = Field(default=30, ge=1, le=10000)
    allow_reuse: bool = False
    engineering_usage_limit: int = Field(default=1, ge=1, le=1000)
    backpressure_threshold: int = Field(default=12, ge=1, le=1000)
    refill_while_paused: bool = False

    @model_validator(mode="after")
    def _continuous_cap(self) -> DispatchRefillSettings:
        if self.mode == "continuous" and self.daily_claim_cap < 1:
            raise ValueError("持续生产必须设置每日领取上限")
        return self


class DispatchWatchdogSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    check_seconds: int = Field(default=30, ge=5, le=600)
    exit_after_seconds: int = Field(default=300, ge=30, le=3600)


class DispatchWindowSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    allow: list[str] = Field(default_factory=lambda: ["00:00-08:00", "20:00-24:00"])


class DispatchSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    engine: Literal["v3", "v2"] = "v3"
    enabled: bool = True
    preset: Literal["throughput", "balanced", "careful", "custom"] = "balanced"
    containers: int = Field(default=4, ge=1, le=8)
    sessions: int = Field(default=0, ge=0, le=8)
    authoring: int = Field(default=2, ge=1, le=8)
    delivering: int = Field(default=1, ge=1, le=8)
    recording: int = Field(default=1, ge=1, le=16)
    in_hand: int = Field(default=0, ge=0, le=100)
    launch_spacing_seconds: int = Field(default=45, ge=10, le=3600)
    session_spacing_seconds: int = Field(default=20, ge=20, le=3600)
    startup_grace_seconds: int = Field(default=20, ge=0, le=300)
    phantom_seconds: int = Field(default=600, ge=30, le=7200)
    docker_timeout_seconds: int = Field(default=20, ge=1, le=300)
    docker_stale_seconds: int = Field(default=15, ge=1, le=300)
    keep_awake: bool = True
    elastic: DispatchElasticSettings = Field(default_factory=DispatchElasticSettings)
    retry: DispatchRetrySettings = Field(default_factory=DispatchRetrySettings)
    guard: DispatchGuardSettings = Field(default_factory=DispatchGuardSettings)
    llm_guard: DispatchLLMGuardSettings = Field(default_factory=DispatchLLMGuardSettings)
    disk: DispatchDiskSettings = Field(default_factory=DispatchDiskSettings)
    refill: DispatchRefillSettings = Field(default_factory=DispatchRefillSettings)
    watchdog: DispatchWatchdogSettings = Field(default_factory=DispatchWatchdogSettings)
    window: DispatchWindowSettings = Field(default_factory=DispatchWindowSettings)

    @model_validator(mode="after")
    def _consistent_limits(self) -> DispatchSettings:
        if self.in_hand and self.in_hand < self.containers:
            raise ValueError("在手上限不能小于容器并发")
        if self.elastic.enabled and self.elastic.ceiling < self.containers:
            raise ValueError("弹性上限不能小于容器数")
        return self


class Settings(BaseSettings):
    """Configuration loaded from ``config.toml`` and a small env override set."""

    model_config = SettingsConfigDict(
        env_prefix="SOLO_AUTO_",
        env_nested_delimiter="__",
        extra="ignore",
        validate_assignment=True,
    )

    host: Literal["127.0.0.1"] = "127.0.0.1"
    port: int = Field(default=8099, ge=1024, le=65535)
    skill_root: Path = Field(default_factory=detect_skill_path)
    tasks_dir: Path = Field(default_factory=_default_tasks_dir)
    source: SourceSettings = Field(default_factory=SourceSettings)
    execution: ExecutionSettings = Field(default_factory=ExecutionSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    recording: RecordingSettings = Field(default_factory=RecordingSettings)
    submission: SubmissionSettings = Field(default_factory=SubmissionSettings)
    autopilot: AutopilotSettings = Field(default_factory=AutopilotSettings)
    audit: AuditSettings = Field(default_factory=AuditSettings)
    intake: IntakeSettings = Field(default_factory=IntakeSettings)
    trash: TrashSettings = Field(default_factory=TrashSettings)
    slim: SlimSettings = Field(default_factory=SlimSettings)
    notify: NotifySettings = Field(default_factory=NotifySettings)
    solo2: Solo2Settings = Field(default_factory=Solo2Settings)
    dispatch: DispatchSettings = Field(default_factory=DispatchSettings)

    _dispatch_migrated: bool = PrivateAttr(default=False)
    _dispatch_validation_error: str = PrivateAttr(default="")

    @field_validator("skill_root", "tasks_dir", mode="before")
    @classmethod
    def _expand_path(cls, value: Any) -> Path:
        return Path(str(value)).expanduser().resolve()


class Paths(BaseModel):
    home: Path
    config: Path
    database: Path
    run: Path
    slots: Path
    staging: Path
    drafts: Path
    exports: Path
    logs: Path
    skill_logs: Path
    backups: Path
    trash: Path
    audits: Path
    batches: Path
    prompts: Path
    autopilot_logs: Path
    pid: Path
    instance_lock: Path

    def ensure(self) -> None:
        for path in (
            self.home,
            self.run,
            self.slots,
            self.staging,
            self.drafts,
            self.exports,
            self.logs,
            self.skill_logs,
            self.backups,
            self.trash,
            self.audits,
            self.batches,
            self.prompts,
            self.autopilot_logs,
        ):
            path.mkdir(parents=True, exist_ok=True)


def app_paths(home: Path | None = None) -> Paths:
    root = (home or Path(os.environ.get("SOLO_AUTO_HOME", "~/solo-auto"))).expanduser().resolve()
    return Paths(
        home=root,
        config=root / "config.toml",
        database=root / "solo-auto.db",
        run=root / "run",
        slots=root / "run" / "slots",
        staging=root / "staging",
        drafts=root / "drafts",
        exports=root / "exports",
        logs=root / "logs",
        skill_logs=root / "logs" / "skill",
        backups=root / "backups",
        trash=root / "trash",
        audits=root / "audits",
        batches=root / "batches",
        prompts=root / "prompts",
        autopilot_logs=root / "logs" / "autopilot",
        pid=root / "run" / "solo-auto.pid",
        instance_lock=root / "run" / "solo-auto.instance.lock",
    )


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, list):
        return json.dumps(value, ensure_ascii=False)
    return json.dumps(str(value), ensure_ascii=False)


def _dump_table(name: str, model: BaseModel, *, exclude: set[str] | None = None) -> list[str]:
    lines = [f"[{name}]"]
    for key, value in model.model_dump().items():
        if exclude and key in exclude:
            continue
        lines.append(f"{key} = {_toml_value(value)}")
    return lines


def config_toml(settings: Settings) -> str:
    lines = [
        f"host = {_toml_value(settings.host)}",
        f"port = {settings.port}",
        f"skill_root = {_toml_value(settings.skill_root)}",
        f"tasks_dir = {_toml_value(settings.tasks_dir)}",
        "",
        *_dump_table("source", settings.source),
        "",
        *_dump_table("execution", settings.execution),
        "",
        *_dump_table("storage", settings.storage),
        "",
        *_dump_table("recording", settings.recording),
        "",
        *_dump_table("submission", settings.submission),
        "",
        *_dump_table("autopilot", settings.autopilot, exclude={"breaker"}),
        "",
        *_dump_table("autopilot.breaker", settings.autopilot.breaker),
        "",
        *_dump_table("audit", settings.audit),
        "",
        *_dump_table("intake", settings.intake),
        "",
        *_dump_table("trash", settings.trash),
        "",
        *_dump_table("slim", settings.slim),
        "",
        *_dump_table("notify", settings.notify, exclude={"webhook_url"}),
        "",
        *_dump_table("solo2", settings.solo2),
        "",
        *_dump_table(
            "dispatch",
            settings.dispatch,
            exclude={
                "elastic",
                "retry",
                "guard",
                "llm_guard",
                "disk",
                "refill",
                "watchdog",
                "window",
            },
        ),
        "",
        *_dump_table("dispatch.elastic", settings.dispatch.elastic),
        "",
        *_dump_table("dispatch.retry", settings.dispatch.retry),
        "",
        *_dump_table("dispatch.guard", settings.dispatch.guard),
        "",
        *_dump_table("dispatch.llm_guard", settings.dispatch.llm_guard),
        "",
        *_dump_table("dispatch.disk", settings.dispatch.disk),
        "",
        *_dump_table("dispatch.refill", settings.dispatch.refill),
        "",
        *_dump_table("dispatch.watchdog", settings.dispatch.watchdog),
        "",
        *_dump_table("dispatch.window", settings.dispatch.window),
        "",
    ]
    return "\n".join(lines)


def _migrated_dispatch(data: dict[str, Any]) -> dict[str, Any]:
    execution = cast(dict[str, Any], data.get("execution")) if isinstance(data.get("execution"), dict) else {}
    autopilot = cast(dict[str, Any], data.get("autopilot")) if isinstance(data.get("autopilot"), dict) else {}
    intake = cast(dict[str, Any], data.get("intake")) if isinstance(data.get("intake"), dict) else {}
    storage = cast(dict[str, Any], data.get("storage")) if isinstance(data.get("storage"), dict) else {}
    containers = int(execution.get("max_containers") or 4)
    authoring = int(autopilot.get("parallel_authoring") or 2)
    delivering = int(autopilot.get("parallel_delivering") or 1)
    sessions = authoring + delivering if {authoring, delivering} != {2, 1} else 0
    in_hand = int(intake.get("max_in_hand") or 0)
    if in_hand and in_hand < containers:
        in_hand = containers
    return {
        "engine": "v3",
        "enabled": bool(autopilot.get("enabled", True)),
        "preset": "custom" if sessions else "balanced",
        "containers": containers,
        "authoring": authoring,
        "delivering": delivering,
        "sessions": min(8, sessions),
        "in_hand": in_hand,
        "elastic": {"ceiling": max(containers, min(8, containers + 2))},
        "disk": {"floor_gb": float(storage.get("disk_floor_gb") or 30.0)},
    }


def load_settings(paths: Paths) -> Settings:
    paths.ensure()
    data: dict[str, Any] = {}
    if paths.config.is_file():
        data = tomllib.loads(paths.config.read_text(encoding="utf-8"))
    if os.environ.get("SOLO_AUTO_PORT"):
        data["port"] = int(os.environ["SOLO_AUTO_PORT"])
    if os.environ.get("SOLO_AUTO_HOST"):
        data["host"] = os.environ["SOLO_AUTO_HOST"]
    if os.environ.get("SOLO_AUTO_TASKS_DIR"):
        data["tasks_dir"] = os.environ["SOLO_AUTO_TASKS_DIR"]
    if os.environ.get("SOLO_AUTO_SKILL_ROOT"):
        data["skill_root"] = os.environ["SOLO_AUTO_SKILL_ROOT"]
    source = dict(data.get("source") or {})
    if os.environ.get("SOLO_AUTO_SOURCE_URL"):
        source["base_url"] = os.environ["SOLO_AUTO_SOURCE_URL"]
    data["source"] = source
    migrated = not isinstance(data.get("dispatch"), dict)
    if migrated:
        data["dispatch"] = _migrated_dispatch(data)
    else:
        try:
            DispatchSettings.model_validate(data["dispatch"])
        except (TypeError, ValueError) as exc:
            LOGGER.warning("invalid [dispatch] config, fallback to defaults: %s", exc)
            data["dispatch"] = DispatchSettings().model_dump()
            settings = Settings(**data)
            settings._dispatch_validation_error = str(exc)
            return settings
    dispatch_env_engine = os.environ.get("SOLO_AUTO_DISPATCH__ENGINE", "").strip()
    if dispatch_env_engine in {"v2", "v3"}:
        data["dispatch"]["engine"] = dispatch_env_engine
    settings = Settings(**data)
    settings._dispatch_migrated = migrated
    return settings


def save_settings(paths: Paths, settings: Settings) -> None:
    text = config_toml(settings)
    tmp = paths.config.with_suffix(".toml.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, paths.config)
