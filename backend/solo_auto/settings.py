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
        pid=root / "run" / "solo-auto.pid",
        instance_lock=root / "run" / "solo-auto.instance.lock",
    )


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)


def _dump_table(name: str, model: BaseModel) -> list[str]:
    lines = [f"[{name}]"]
    for key, value in model.model_dump().items():
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
