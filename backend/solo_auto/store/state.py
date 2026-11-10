"""Pydantic models for the skill-owned ``monitor/state.json``."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA: Literal["solo-2611/1"] = "solo-2611/1"


class SourceState(BaseModel):
    model_config = ConfigDict(extra="allow")

    kind: str = ""
    path: str = ""
    url: str = ""
    ref: str = ""
    head: str = ""


class AttemptState(BaseModel):
    model_config = ConfigDict(extra="allow")

    index: int
    container: str = ""
    mode: str = ""
    image: str = ""
    startedAt: str = ""
    status: str = ""
    dir: str = ""
    exitCode: int | None = None
    finishedAt: str = ""
    errors: list[str] = Field(default_factory=list)
    result: dict[str, Any] = Field(default_factory=dict)
    timeout: bool = False


class RemoteState(BaseModel):
    model_config = ConfigDict(extra="allow")

    fullName: str = ""
    web: str = ""
    url: str = ""
    branch: str = "main"
    private: bool = False
    createdAt: str = ""
    initialPushed: str = ""
    artifactSha: str = ""
    artifactPushed: str = ""
    verified: bool | dict[str, Any] = False


class SessionState(BaseModel):
    model_config = ConfigDict(extra="allow")

    sessionId: str = ""
    promptId: str = ""
    harnessVersion: str = ""
    entrypoint: str = ""
    models: list[str] = Field(default_factory=list)
    trace: str = ""
    tracesDir: str = ""
    subagentFiles: int = 0
    toolUses: int = 0
    warnings: list[str] = Field(default_factory=list)
    mode: str = ""


class ArtifactState(BaseModel):
    model_config = ConfigDict(extra="allow")

    sha: str = ""
    modelCommits: list[str] = Field(default_factory=list)
    shortStat: str = ""
    stat: str = ""
    warning: str = ""
    largeBinaries: list[str] = Field(default_factory=list)
    createdAt: str = ""


class RecordingState(BaseModel):
    model_config = ConfigDict(extra="allow")

    video: str = ""
    durationSeconds: float = 0
    width: int = 0
    height: int = 0
    sizeMB: float = 0
    needsObjectStorage: bool = False
    source: str = ""
    finishedAt: str = ""
    captureKind: str = ""
    captureBackend: str = ""
    windowCaptureOk: bool = False
    pointerStrategy: str = ""
    activationPerformed: bool = False
    recordPlan: str | None = None
    recordPlanMode: str | None = None


class FeedbackCheckState(BaseModel):
    model_config = ConfigDict(extra="allow")

    ok: bool = False
    score: int | None = None
    warnings: list[str] = Field(default_factory=list)
    sha256: str = ""
    checkedAt: str = ""


class ExportState(BaseModel):
    model_config = ConfigDict(extra="allow")

    xlsx: str = ""
    guide: str = ""
    missing: list[str] = Field(default_factory=list)
    exportedAt: str = ""
    exportedDate: str = ""


class SoloState(BaseModel):
    model_config = ConfigDict(extra="allow")

    schema_name: Literal["solo-2611/1"] = Field(default=SCHEMA, alias="schema")
    skillVersion: str = ""
    taskName: str = ""
    taskRoot: str = ""
    workspace: str = ""
    createdAt: str = ""
    createdDate: str = ""
    status: str = ""
    source: SourceState = Field(default_factory=SourceState)
    initialSha: str = ""
    fields: dict[str, Any] = Field(default_factory=dict)
    attempts: list[AttemptState] = Field(default_factory=list)
    updatedAt: str = ""
    promptPath: str = ""
    promptSha256: str = ""
    promptReview: str = ""
    difficultyReview: str = ""
    difficultySignalsPassed: int | bool = False
    remote: RemoteState = Field(default_factory=RemoteState)
    session: SessionState | None = None
    artifact: ArtifactState | None = None
    recordPlan: dict[str, Any] | None = None
    recording: RecordingState | None = None
    feedbackCheck: FeedbackCheckState | None = None
    export: ExportState | None = None
    pendingContainer: str = ""


class StateEnvelope(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    compatible: bool
    raw: dict[str, Any]
    state: SoloState | None
    path: Path
    mtime: float
    error: str = ""


def read_json(path: Path, default: Any = None) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def load_state(task_root: Path) -> StateEnvelope:
    path = task_root / "monitor" / "state.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        mtime = path.stat().st_mtime
    except FileNotFoundError:
        return StateEnvelope(
            compatible=False,
            raw={},
            state=None,
            path=path,
            mtime=0,
            error="缺少 monitor/state.json",
        )
    except (OSError, json.JSONDecodeError) as exc:
        return StateEnvelope(
            compatible=False,
            raw={},
            state=None,
            path=path,
            mtime=0,
            error=f"state.json 无法解析: {exc}",
        )
    if not isinstance(raw, dict):
        return StateEnvelope(
            compatible=False,
            raw={},
            state=None,
            path=path,
            mtime=mtime,
            error="state.json 顶层不是对象",
        )
    if raw.get("schema") != SCHEMA:
        return StateEnvelope(
            compatible=False,
            raw=raw,
            state=None,
            path=path,
            mtime=mtime,
            error=f"未知 schema: {raw.get('schema')!r}，进入只读兼容模式",
        )
    try:
        state = SoloState.model_validate(raw)
    except Exception as exc:
        return StateEnvelope(
            compatible=False,
            raw=raw,
            state=None,
            path=path,
            mtime=mtime,
            error=f"state.json 字段校验失败: {exc}",
        )
    return StateEnvelope(compatible=True, raw=raw, state=state, path=path, mtime=mtime)


def iso_now() -> str:
    return datetime.now().astimezone().replace(microsecond=0).isoformat()
