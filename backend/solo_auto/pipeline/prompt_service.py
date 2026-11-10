"""Prompt draft, local preview and skill-gated installation."""

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

NON_SPACE = re.compile(r"\s+")


class PromptService:
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

    def draft_path(self, task_id: str) -> Path:
        return self.paths.drafts / task_id / "题面草稿.md"

    def review_path(self, task_id: str, kind: str) -> Path:
        names = {
            "难度论证": "difficulty-review.json",
            "difficulty": "difficulty-review.json",
            "ra-人话审核": "ra-renhua-review.json",
            "ra-renhua": "ra-renhua-review.json",
        }
        name = names.get(kind)
        if not name:
            raise ApiError(400, "REVIEW_KIND_INVALID", "复核类型必须是「难度论证」或「ra-人话审核」")
        return self.paths.drafts / task_id / name

    def get(self, task_id: str) -> dict[str, Any]:
        task = self._task(task_id)
        root = Path(task["task_root"])
        installed_path = root / "delivery" / "提示词.md"
        installed = _read_text(installed_path)
        draft = _read_text(self.draft_path(task_id)) or installed
        envelope = load_state(root)
        attempts = len(envelope.state.attempts) if envelope.state else 0
        reopen_path = self.paths.drafts / task_id / "reopen.json"
        reopened = bool(
            installed_path.is_file()
            and reopen_path.is_file()
            and reopen_path.stat().st_mtime > installed_path.stat().st_mtime
        )
        readonly = bool(installed and (attempts > 0 or not reopened))
        return {
            "draft": draft,
            "installed": installed,
            "installed_sha256": envelope.state.promptSha256 if envelope.state else "",
            "readonly": readonly,
            "can_reopen": bool(installed and attempts == 0 and readonly),
            "chars": _chars(draft),
            "limits": {"preferred": 170, "hard": 240},
        }

    def save_draft(self, task_id: str, text: str) -> dict[str, Any]:
        task = self._task(task_id)
        root = Path(task["task_root"])
        envelope = load_state(root)
        if envelope.state and envelope.state.attempts and envelope.state.promptPath:
            raise ApiError(409, "PROMPT_LOCKED", "题面已运行，不能修改；请重开新题")
        path = self.draft_path(task_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        self.repo.add_event(
            "prompt.draft_saved",
            f"题面草稿已保存（{_chars(text)} 字）",
            actor="user",
            task_id=task_id,
        )
        return self.preview(text)

    def preview(self, text: str) -> dict[str, Any]:
        chars = _chars(text)
        errors: list[str] = []
        warnings: list[str] = []
        if not text.strip():
            errors.append("提示词为空")
        if chars > 240:
            errors.append(f"题面 {chars} 字，超过绝对上限 240 字")
        elif chars > 170:
            warnings.append(f"题面 {chars} 字，超过优先上限 170 字；确认确有需要")
        if chars < 80:
            warnings.append("提示词不足 80 字，容易被判为过于简单")
        scan = getattr(self.spec, "scan_banned", None)
        if callable(scan):
            blocked, warned = scan(text, "数据工程")
            if blocked:
                errors.append(f"命中不被允许的雷同题：{blocked}")
            if warned:
                warnings.append(f"含敏感词 {warned}，确认不是雷同题")
        if re.search(r"(?:^|\s)(?:/[A-Za-z0-9_.-]+|[A-Za-z]:\\\\)", text):
            errors.append("题面包含文件路径，请改成业务描述")
        if re.search(r"(?:localhost|127\.0\.0\.1|端口\s*\d+)", text, re.I):
            errors.append("题面包含端口或本机地址，请改成业务描述")
        return {"ok": not errors, "chars": chars, "errors": errors, "warnings": warnings}

    async def install(
        self,
        task_id: str,
        *,
        task_type: str,
        difficulty: str,
        languages: str,
        repro_level: str | None,
        domain: str,
        sub_direction: str,
        allow_over_170: bool,
    ) -> dict[str, Any]:
        task = self._task(task_id)
        root = Path(task["task_root"])
        draft = self.draft_path(task_id)
        if not draft.is_file() or not draft.read_text(encoding="utf-8").strip():
            raise ApiError(422, "PROMPT_EMPTY", "题面草稿为空，不能安装")
        review = self.review_path(task_id, "ra-人话审核")
        difficulty_review = self.review_path(task_id, "难度论证")
        argv = [
            "prompt",
            "--task-root",
            str(root),
            "--file",
            str(draft),
            "--task-type",
            task_type,
            "--difficulty",
            difficulty,
            "--lang",
            languages,
            "--domain",
            domain,
            "--sub-direction",
            sub_direction,
            "--review",
            str(review),
            "--difficulty-review",
            str(difficulty_review),
        ]
        if allow_over_170:
            argv.append("--allow-over-170")
        result = await self.gateway.call(argv, timeout=300, task_id=task_id)
        data = result.data or {}
        if result.code == 2:
            errors = [str(item) for item in data.get("errors") or []]
            warnings = [str(item) for item in data.get("warnings") or []]
            self.repo.add_event(
                "gate.prompt_failed",
                "；".join(errors) or result.err or "题面门禁未通过",
                level="warn",
                actor="gateway",
                task_id=task_id,
                details={"errors": errors, "warnings": warnings},
            )
            return {
                "ok": False,
                "errors": errors,
                "warnings": warnings,
                "stderr": result.err,
                "installed_sha256": "",
            }
        if result.code != 0 or data.get("ok") is not True:
            raise ApiError(
                500,
                "PROMPT_INSTALL_FAILED",
                result.err.strip() or "题面安装失败",
                detail={"stdout": result.out, "stderr": result.err, "exit_code": result.code},
            )
        await self.reconciler.run()
        self.bus.publish("task.updated", {"task_id": task_id, "job_status": "prompt_ok", "step": 3})
        return {
            "ok": True,
            "errors": data.get("errors") or [],
            "warnings": data.get("warnings") or [],
            "installed_sha256": data.get("sha256") or "",
            "stderr": result.err,
        }

    def reviews(self, task_id: str) -> dict[str, Any]:
        task = self._task(task_id)
        envelope = load_state(Path(task["task_root"]))
        difficulty = _read_json(self.review_path(task_id, "难度论证"))
        ra_renhua = _read_json(self.review_path(task_id, "ra-人话审核"))
        if envelope.state:
            if not difficulty and envelope.state.difficultyReview:
                difficulty = _read_json(Path(envelope.state.difficultyReview))
            if not ra_renhua and envelope.state.promptReview:
                ra_renhua = _read_json(Path(envelope.state.promptReview))
        return {"difficulty": difficulty, "ra_renhua": ra_renhua}

    def save_review(self, task_id: str, kind: str, data: dict[str, Any]) -> dict[str, Any]:
        self._task(task_id)
        path = self.review_path(task_id, kind)
        if not isinstance(data, dict):
            raise ApiError(422, "REVIEW_INVALID", "复核记录必须是 JSON 对象")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self.repo.add_event("review.saved", f"{kind} 已保存", actor="user", task_id=task_id)
        return data

    def reopen(self, task_id: str, reason: str) -> dict[str, Any]:
        reason = reason.strip()
        if not reason:
            raise ApiError(422, "REOPEN_REASON_REQUIRED", "重开本题必须填写原因")
        task = self._task(task_id)
        envelope = load_state(Path(task["task_root"]))
        if envelope.state and envelope.state.attempts:
            raise ApiError(409, "PROMPT_CANNOT_REOPEN", "已运行任务不能重开题面；请换新题")
        marker = self.paths.drafts / task_id / "reopen.json"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            json.dumps({"reason": reason, "at": _now()}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        self.repo.add_event("prompt.reopened", reason, level="warn", actor="user", task_id=task_id)
        return {"ok": True, "reason": reason}

    def _task(self, task_id: str) -> dict[str, Any]:
        task = self.repo.get_task(task_id)
        if not task:
            raise ApiError(404, "TASK_NOT_FOUND", f"任务不存在：{task_id}")
        return task


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _chars(text: str) -> int:
    return len(NON_SPACE.sub("", text or ""))


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _now() -> str:
    from datetime import datetime

    return datetime.now().astimezone().replace(microsecond=0).isoformat()
