"""Feedback editing boundary: exact storage and skill-owned checking."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from types import ModuleType
from typing import Any

from solo_auto.errors import ApiError
from solo_auto.events.bus import EventBus
from solo_auto.gateway.skill import SkillGateway
from solo_auto.pipeline.reconcile import Reconciler
from solo_auto.settings import Paths
from solo_auto.store.repo import Repository
from solo_auto.store.state import load_state

SECTION = re.compile(r"^##\s*(交付完整性|Feedback|备注)\s*$", re.M)
PLACEHOLDER = re.compile(r"（填 1–5 的整数）|（可空[^）]*）")
PROCESS = re.compile(r"(过程|第\s*\d+\s*步|轨迹|调用|执行了|读取|运行了|验证|规划|反复|重试)")
ARTIFACT = re.compile(r"(产物|页面|画面|输出|文件|接口|功能|效果|运行后|打开|结果|记录|批次|对账|入库|回读)")
NUMBER = re.compile(r"(?<!\w)\d+(?:[./:-]\d+)*(?:%|ms|s|MB|GB)?(?!\w)")


class FeedbackService:
    def __init__(
        self,
        paths: Paths,
        gateway: SkillGateway,
        repo: Repository,
        reconciler: Reconciler,
        bus: EventBus,
        spec: ModuleType,
    ) -> None:
        self.paths = paths
        self.gateway = gateway
        self.repo = repo
        self.reconciler = reconciler
        self.bus = bus
        self.spec = spec

    async def init(self, task_id: str) -> str:
        task = self._task(task_id)
        root = Path(task["task_root"])
        result = await self.gateway.call(
            ["feedback", "--task-root", str(root), "--init"],
            timeout=60,
            task_id=task_id,
        )
        if result.code != 0:
            raise ApiError(
                500,
                "FEEDBACK_INIT_FAILED",
                result.err.strip() or "Feedback 模板生成失败",
                detail={"stdout": result.out, "stderr": result.err},
            )
        path = root / "delivery" / "feedback.md"
        self.repo.add_event("feedback.initialized", "Feedback 模板已准备", actor="user", task_id=task_id)
        return str(path)

    def get(self, task_id: str) -> dict[str, Any]:
        task = self._task(task_id)
        root = Path(task["task_root"])
        path = root / "delivery" / "feedback.md"
        text = path.read_text(encoding="utf-8") if path.is_file() else ""
        parts = _parse_sections(text)
        feedback = parts.get("Feedback", "")
        body = re.sub(r"(过程问题|产物问题)[:：]", "", feedback).strip()
        checked = _read_json(root / "monitor" / "feedback-check.json")
        envelope = load_state(root)
        ack = _read_json(root / "monitor" / "review-ack.json")
        current_sha = _hash(path) if path.is_file() else ""
        checked_sha = str(
            checked.get("sha256")
            or (envelope.state.feedbackCheck.sha256 if envelope.state and envelope.state.feedbackCheck else "")
        )
        anchors = len(self.spec.ANCHOR_PATTERN.findall(feedback))
        digit_chars = sum(character.isdigit() for character in body)
        chars = _non_space(body)
        return {
            "text": text,
            "chars": chars,
            "anchors": anchors,
            "digits_ratio": round(digit_chars / max(1, chars), 4),
            "coverage": {
                "process": bool(PROCESS.search(feedback)),
                "artifact": bool(ARTIFACT.search(feedback)),
            },
            "checked": {
                "ok": bool(checked.get("ok")),
                "score": checked.get("score"),
                "errors": checked.get("errors") or [],
                "warnings": checked.get("warnings") or [],
                "stale": bool(checked_sha and current_sha and checked_sha != current_sha),
            },
            "review_ack": {
                **ack,
                "valid": bool(ack.get("acked") and ack.get("feedbackSha256") == current_sha),
            },
        }

    def save(self, task_id: str, text: str) -> dict[str, Any]:
        task = self._task(task_id)
        root = Path(task["task_root"])
        path = root / "delivery" / "feedback.md"
        if not path.exists():
            raise ApiError(409, "FEEDBACK_NOT_INITIALIZED", "请先生成 Feedback 模板")
        path.write_text(text, encoding="utf-8")
        draft = self.paths.drafts / task_id / "feedback.md"
        draft.parent.mkdir(parents=True, exist_ok=True)
        draft.write_text(text, encoding="utf-8")
        self.repo.add_event(
            "feedback.saved",
            f"Feedback 原样保存（{_non_space(text)} 字）",
            actor="user",
            task_id=task_id,
        )
        return self.get(task_id)

    async def check(self, task_id: str) -> dict[str, Any]:
        task = self._task(task_id)
        root = Path(task["task_root"])
        result = await self.gateway.call(
            ["feedback", "--task-root", str(root), "--check"],
            timeout=120,
            task_id=task_id,
        )
        data = result.data or {}
        if result.code not in {0, 2}:
            raise ApiError(
                500,
                "FEEDBACK_CHECK_FAILED",
                result.err.strip() or "Feedback 检查执行失败",
                detail={"stdout": result.out, "stderr": result.err, "exit_code": result.code},
            )
        await self.reconciler.run()
        self.bus.publish(
            "task.updated",
            {"task_id": task_id, "gate": "feedback", "ok": bool(data.get("ok")), "step": 10},
        )
        return {
            "ok": bool(data.get("ok")),
            "score": data.get("score"),
            "errors": data.get("errors") or [],
            "warnings": data.get("warnings") or [],
            "stderr": result.err,
            "exit_code": result.code,
        }

    def ack(self, task_id: str, *, acked: bool) -> dict[str, Any]:
        task = self._task(task_id)
        root = Path(task["task_root"])
        if not acked:
            raise ApiError(422, "REVIEW_ACK_REQUIRED", "必须确认已逐条复核事实与措辞")
        feedback = root / "delivery" / "feedback.md"
        checked = _read_json(root / "monitor" / "feedback-check.json")
        envelope = load_state(root)
        state_check = envelope.state.feedbackCheck if envelope.state else None
        checked_sha = str(checked.get("sha256") or (state_check.sha256 if state_check else ""))
        current_sha = _hash(feedback) if feedback.is_file() else ""
        checked_ok = bool(checked.get("ok") or (state_check and state_check.ok))
        if not checked_ok:
            raise ApiError(422, "FEEDBACK_GATE_FAILED", "Feedback 检查未通过，不能确认复核")
        if not current_sha or current_sha != checked_sha:
            raise ApiError(409, "FEEDBACK_CHANGED", "Feedback 在检查后被修改，请重新检查")
        payload = {
            "schemaVersion": 1,
            "acked": True,
            "ackedAt": _now(),
            "feedbackSha256": current_sha,
            "chars": _non_space(feedback.read_text(encoding="utf-8")),
            "score": checked.get("score") or (state_check.score if state_check else None),
        }
        path = root / "monitor" / "review-ack.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self.repo.add_event("feedback.reviewed", "已确认逐条复核", actor="user", task_id=task_id)
        return payload

    def _task(self, task_id: str) -> dict[str, Any]:
        task = self.repo.get_task(task_id)
        if not task:
            raise ApiError(404, "TASK_NOT_FOUND", f"任务不存在：{task_id}")
        return task


def _parse_sections(text: str) -> dict[str, str]:
    cleaned = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    matches = list(SECTION.finditer(cleaned))
    parts: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(cleaned)
        label = match.group(1)
        parts[label] = PLACEHOLDER.sub("", cleaned[match.end() : end]).strip()
    return parts


def _non_space(text: str) -> int:
    return len(re.sub(r"\s+", "", text or ""))


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _now() -> str:
    from datetime import datetime

    return datetime.now().astimezone().replace(microsecond=0).isoformat()
