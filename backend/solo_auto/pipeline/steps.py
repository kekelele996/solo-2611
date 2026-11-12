"""The authoritative 11-step delivery evaluation."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from solo_auto.store.state import SoloState, load_state

STEP_NAMES = (
    "初始快照",
    "GitHub 初始快照",
    "提示词（难度+人话）",
    "单轮会话",
    "TurnID/PromptID",
    "产物快照",
    "GitHub 产物快照",
    "轨迹时间线",
    "运行录屏",
    "Feedback（证据化并复核）",
    "Excel",
)

FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", re.I)


@dataclass(slots=True)
class Step:
    number: int
    key: str
    name: str
    state: str
    detail: str
    ok: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Evaluation:
    steps: list[Step]
    complete: bool
    missing: list[str]


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return ""
    return digest.hexdigest()


def _permalink(state: SoloState, sha: str) -> str:
    if not sha:
        return ""
    if state.remote.web:
        return f"{state.remote.web.rstrip('/')}/commit/{sha}"
    if state.remote.fullName:
        return f"https://github.com/{state.remote.fullName}/commit/{sha}"
    return ""


def _recording_evaluation(task_root: Path, state: SoloState) -> tuple[bool, str]:
    rec = state.recording
    if rec is None or not rec.video:
        return False, "未录制"
    video = Path(rec.video)
    if not video.is_file():
        return False, "录屏文件不存在"
    capture = _read_json(task_root / "recording" / "运行录屏-window-capture.json")
    cursor = _read_json(task_root / "recording" / "运行录屏-cursor-guard.json")
    chrome = _read_json(task_root / "recording" / "chrome-instance.json")
    cleanup = _read_json(task_root / "recording" / "chrome-profile-cleanup.json")
    if rec.durationSeconds <= 0 or rec.durationSeconds > 90:
        return False, f"时长 {rec.durationSeconds:g}s 超出 1–90 秒范围"
    if (rec.width, rec.height) != (1280, 720):
        return False, f"分辨率 {rec.width}×{rec.height}，要求 1280×720"
    if rec.windowCaptureOk is not True:
        return False, "窗口采集标记未通过"
    normal = capture.get("status") == "ok" and cursor.get("status") == "ok"
    manual = capture.get("status") == cursor.get("status") == "manual-import"
    if not (normal or manual):
        return False, "capture 或 cursor guard 未通过"
    if cursor.get("pointerStrategy") != "none":
        return False, "pointerStrategy 必须为 none"
    if rec.recordPlanMode == "web" or capture.get("ownerApp") == "Chrome":
        web_ok = (
            chrome.get("status") == "ok"
            and chrome.get("dedicatedInstance") is True
            and chrome.get("reusedRunningChrome") is False
            and chrome.get("userChromeTouched") is False
            and chrome.get("debugPortOwnerVerified") is True
            and cleanup.get("status") == "removed"
            and cleanup.get("existsAfter") is False
        )
        if not web_ok:
            return False, "Web 独立 Chrome 或临时 profile 清理证据未通过"
    return True, f"{rec.durationSeconds:g}s 1280×720 {rec.captureKind}"


def _is_old_exported_review_legacy(state: SoloState) -> bool:
    return bool(state.export and state.export.missing == [] and state.export.exportedAt)


def evaluate_steps(
    task_root: Path,
    *,
    duplicate_sessions: dict[str, int] | None = None,
    require_review_ack: bool = True,
) -> Evaluation:
    envelope = load_state(task_root)
    if not envelope.compatible or envelope.state is None:
        detail = envelope.error or "状态不可读"
        blocked_steps = [
            Step(index, name, name, "blocked", detail if index == 1 else "", False)
            for index, name in enumerate(STEP_NAMES, 1)
        ]
        return Evaluation(steps=blocked_steps, complete=False, missing=[detail])
    state = envelope.state
    session_id = state.session.sessionId if state.session else ""
    prompt_id = state.session.promptId if state.session else ""
    prompt_path = Path(state.promptPath) if state.promptPath else None
    validation = _read_json(task_root / "monitor" / "prompt" / "validation.json")
    prompt_ok = bool(
        prompt_path
        and prompt_path.is_file()
        and state.promptSha256
        and _sha256(prompt_path) == state.promptSha256
        and validation.get("ok") is True
    )
    remote_initial = _permalink(state, state.initialSha)
    artifact_sha = state.artifact.sha if state.artifact else ""
    remote_artifact = _permalink(state, artifact_sha)
    recording_ok, recording_detail = _recording_evaluation(task_root, state)
    feedback_check = _read_json(task_root / "monitor" / "feedback-check.json")
    ack = _read_json(task_root / "monitor" / "review-ack.json")
    feedback_file = task_root / "delivery" / "feedback.md"
    checked_sha = str(state.feedbackCheck.sha256 if state.feedbackCheck else "")
    ack_sha = str(ack.get("feedbackSha256") or "")
    file_sha = _sha256(feedback_file) if feedback_file.is_file() else ""
    ack_ok = bool(ack.get("acked") is True and ack_sha and file_sha and ack_sha == file_sha)
    machine_feedback_ok = bool((state.feedbackCheck and state.feedbackCheck.ok) or feedback_check.get("ok") is True)
    feedback_ok = machine_feedback_ok
    if require_review_ack and not ack_ok and not _is_old_exported_review_legacy(state):
        feedback_ok = False
        feedback_detail = "机器检查已过，但尚未完成人工复核确认" if machine_feedback_ok else ""
    else:
        feedback_detail = (
            f"score={state.feedbackCheck.score}"
            if state.feedbackCheck and state.feedbackCheck.score is not None
            else ""
        )
    if checked_sha and file_sha and checked_sha != file_sha and not ack_ok:
        feedback_ok = False
        feedback_detail = "feedback.md 检查后又被修改，请重新检查"
    duplicate = bool(session_id and duplicate_sessions and duplicate_sessions.get(session_id, 0) > 1)
    export_ok = bool(
        state.export and state.export.missing == [] and state.export.xlsx and Path(state.export.xlsx).is_file()
    )
    checks: list[tuple[bool, str, str]] = [
        (bool(FULL_SHA.fullmatch(state.initialSha or "")), state.initialSha, "初始快照必须是完整 40 位 SHA"),
        (bool(remote_initial), remote_initial, "初始快照尚未推送"),
        (prompt_ok, state.fields.get("任务类型", ""), "提示词验证未通过或安装后被修改"),
        (bool(session_id and UUID.fullmatch(session_id)), session_id, "SessionID 缺失或格式错误"),
        (
            bool(session_id and UUID.fullmatch(session_id) and prompt_id and not duplicate),
            "重复 SessionID" if duplicate else prompt_id,
            "SessionID 重复，禁止导出" if duplicate else "PromptID 缺失",
        ),
        (
            bool(artifact_sha and artifact_sha != state.initialSha),
            state.artifact.shortStat if state.artifact else "",
            "产物快照缺失或与初始快照相同",
        ),
        (bool(remote_artifact and state.remote.artifactSha), remote_artifact, "产物快照尚未推送"),
        ((task_root / "delivery" / "轨迹时间线.md").is_file(), "", "轨迹时间线尚未生成"),
        (recording_ok, recording_detail, recording_detail),
        (feedback_ok, feedback_detail, feedback_detail or "Feedback 检查未通过"),
        (export_ok, state.export.xlsx if state.export else "", "Excel 尚未生成"),
    ]
    steps: list[Step] = []
    current_assigned = False
    missing: list[str] = []
    for number, (item, name) in enumerate(zip(checks, STEP_NAMES, strict=True), 1):
        ok, detail, failure = item
        if not ok:
            state_name = "current" if not current_assigned else "pending"
            current_assigned = True
            missing.append(f"{number}. {name}：{failure}")
        else:
            state_name = "done"
        steps.append(Step(number, name, name, state_name, detail, ok))
    complete = all(step.ok for step in steps)
    return Evaluation(steps=steps, complete=complete, missing=missing)


def derive_job_status(
    state: SoloState,
    *,
    container_live: bool = False,
    review_acked: bool = False,
    recording_ok: bool = False,
) -> str:
    status = state.status
    if status == "prepared":
        return "authored"
    if status == "prompt_ready":
        return "ready" if state.remote.initialPushed else "prompt_ok"
    if status == "running":
        return "running" if container_live else "blocked"
    if status in {"ran", "snapshotted"}:
        return "ran"
    if status == "recorded":
        if not recording_ok:
            return "ran"
        feedback_ok = bool(state.feedbackCheck and state.feedbackCheck.ok)
        return "reviewed" if feedback_ok and review_acked else "recorded"
    if status == "exported":
        return "exported"
    if status == "run_failed":
        return "blocked"
    return "blocked"
