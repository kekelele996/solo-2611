"""Filesystem layout and editable application settings."""

from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    autopilot: AutopilotSettings = Field(default_factory=AutopilotSettings)
    audit: AuditSettings = Field(default_factory=AuditSettings)
    intake: IntakeSettings = Field(default_factory=IntakeSettings)
    trash: TrashSettings = Field(default_factory=TrashSettings)
    slim: SlimSettings = Field(default_factory=SlimSettings)
    notify: NotifySettings = Field(default_factory=NotifySettings)

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
    ]
    return "\n".join(lines)


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
    return Settings(**data)


def save_settings(paths: Paths, settings: Settings) -> None:
    text = config_toml(settings)
    tmp = paths.config.with_suffix(".toml.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, paths.config)
