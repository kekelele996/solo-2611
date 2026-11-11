"""Window recording orchestration around the skill recorder."""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from solo_auto.errors import ApiError
from solo_auto.events.bus import EventBus
from solo_auto.gateway.skill import SkillGateway
from solo_auto.pipeline.reconcile import Reconciler
from solo_auto.settings import Paths, RecordingSettings
from solo_auto.store.repo import Repository
from solo_auto.store.state import load_state

WINDOW_LINE = re.compile(r"^\s*(\d+)\s{2}(.{1,22}?)\s{2}(\d+)x(\d+)\s+(.*)$")


class RecordingService:
    def __init__(
        self,
        *,
        paths: Paths,
        settings: RecordingSettings,
        gateway: SkillGateway,
        repo: Repository,
        reconciler: Reconciler,
        bus: EventBus,
    ) -> None:
        self.paths = paths
        self.settings = settings
        self.gateway = gateway
        self.repo = repo
        self.reconciler = reconciler
        self.bus = bus
        self.pending_task_id = ""
        self.pending_started_at = 0.0
        self.pending: asyncio.Task[None] | None = None
        self.active_task_id = ""
        self.active_started_at = 0.0
        self._auto_stop_task: asyncio.Task[None] | None = None

    async def windows(self) -> list[dict[str, Any]]:
        result = await self.gateway.call(["record", "list"], timeout=60)
        if result.code != 0:
            raise ApiError(
                503,
                "WINDOW_LIST_FAILED",
                result.err.strip() or "读取窗口失败",
                detail={"stdout": result.out, "stderr": result.err},
            )
        return parse_window_list(result.out)

    async def get_plan(self, task_id: str) -> dict[str, Any]:
        task = self._task(task_id)
        path = Path(task["task_root"]) / "monitor" / "record-plan.json"
        plan = _read_json(path)
        checked: dict[str, Any] = {"ok": False, "errors": ["录屏计划尚未生成"]}
        if path.is_file():
            result = await self.gateway.call(
                ["record", "check-plan", "--task-root", str(task["task_root"]), "--file", str(path)],
                timeout=60,
                task_id=task_id,
            )
            checked = result.data or {"ok": False, "errors": [result.err.strip() or "计划未通过"]}
        return {"plan": plan, "exists": path.is_file(), "checked": checked, "path": str(path)}

    async def save_plan(self, task_id: str, plan: dict[str, Any]) -> dict[str, Any]:
        task = self._task(task_id)
        if not isinstance(plan, dict):
            raise ApiError(422, "RECORD_PLAN_INVALID", "录屏计划必须是 JSON 对象")
        root = Path(task["task_root"])
        path = root / "monitor" / "record-plan.json"
        path.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        result = await self.gateway.call(
            ["record", "check-plan", "--task-root", str(root), "--file", str(path)],
            timeout=60,
            task_id=task_id,
        )
        checked = result.data or {"ok": False, "errors": [result.err.strip() or "计划未通过"]}
        self.repo.add_event(
            "record.plan_checked",
            "录屏计划已保存并通过" if checked.get("ok") else "录屏计划已保存但未通过",
            level="info" if checked.get("ok") else "warn",
            actor="user",
            task_id=task_id,
            details=checked,
        )
        return {"plan": plan, "exists": True, "checked": checked, "path": str(path)}

    async def start(
        self,
        task_id: str,
        *,
        window_id: int,
        plan: str | None = None,
    ) -> dict[str, Any]:
        self._task(task_id)
        if self.pending_task_id or self.active_task_id:
            owner = self.pending_task_id or self.active_task_id
            raise ApiError(409, "RECORDING_BUSY", f"已有任务正在录屏：{owner}")
        if window_id <= 0:
            raise ApiError(422, "WINDOW_REQUIRED", "必须显式选择窗口")
        task = self._task(task_id)
        root = Path(task["task_root"])
        plan_path = Path(plan).resolve() if plan else root / "monitor" / "record-plan.json"
        if not plan_path.is_file():
            raise ApiError(422, "RECORD_PLAN_MISSING", "请先生成并保存录屏计划")
        self.pending_task_id = task_id
        self.pending_started_at = time.monotonic()
        self.pending = asyncio.create_task(self._start_after_countdown(task_id, root, plan_path, window_id))
        self.repo.add_event("record.countdown", "录屏 3 秒倒计时开始", actor="user", task_id=task_id)
        return {
            "recording": True,
            "pending": True,
            "countdown": 3,
            "task_id": task_id,
            "window_id": window_id,
        }

    async def _start_after_countdown(self, task_id: str, root: Path, plan: Path, window_id: int) -> None:
        try:
            await asyncio.sleep(3)
            result = await self.gateway.call(
                [
                    "record",
                    "start",
                    "--task-root",
                    str(root),
                    "--window-id",
                    str(window_id),
                    "--max",
                    str(self.settings.max_seconds),
                    "--plan",
                    str(plan),
                ],
                timeout=60,
                task_id=task_id,
            )
            if result.code != 0:
                detail = result.err.strip() or result.out.strip() or "录屏启动失败"
                self.repo.add_event("record.failed", detail, level="error", actor="gateway", task_id=task_id)
                self.bus.publish("toast", {"level": "error", "message": detail[:500], "task_id": task_id})
                self.active_task_id = ""
                return
            self.active_task_id = task_id
            self.active_started_at = time.monotonic()
            self._auto_stop_task = asyncio.create_task(self._auto_stop(task_id))
            self.repo.add_event("record.started", f"正在录制窗口 {window_id}", actor="user", task_id=task_id)
            self.bus.publish("record.updated", {"task_id": task_id, "recording": True})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.repo.add_event("record.failed", str(exc), level="error", actor="gateway", task_id=task_id)
        finally:
            self.pending_task_id = ""

    async def status(self) -> dict[str, Any]:
        if self.pending_task_id:
            elapsed = time.monotonic() - self.pending_started_at
            return {
                "recording": False,
                "pending": True,
                "task_id": self.pending_task_id,
                "countdown_remaining": max(0, 3 - int(elapsed)),
                "elapsed_s": round(elapsed, 1),
                "remaining_s": self.settings.max_seconds,
            }
        if self.active_task_id:
            elapsed = time.monotonic() - self.active_started_at
            return {
                "recording": True,
                "pending": False,
                "task_id": self.active_task_id,
                "started_at": datetime.fromtimestamp(self.active_started_at).astimezone().isoformat(),
                "elapsed_s": round(elapsed, 1),
                "remaining_s": max(0, self.settings.max_seconds - int(elapsed)),
            }
        lock = _read_json(Path.home() / ".codex" / "solo-2611" / "run" / "recording.lock.json")
        if lock.get("status") in {"starting", "recording"} and _pid_alive(int(lock.get("ownerPid") or 0)):
            return {
                "recording": True,
                "pending": lock.get("status") == "starting",
                "task_id": _task_id_for_root(self.repo, str(lock.get("taskRoot") or "")),
                "elapsed_s": 0,
                "remaining_s": self.settings.max_seconds,
                "external": True,
            }
        return {
            "recording": False,
            "pending": False,
            "task_id": "",
            "elapsed_s": 0,
            "remaining_s": self.settings.max_seconds,
        }

    async def stop(self, task_id: str | None = None) -> dict[str, Any]:
        target_id = task_id or self.active_task_id
        if self._auto_stop_task and not self._auto_stop_task.done():
            self._auto_stop_task.cancel()
            await asyncio.gather(self._auto_stop_task, return_exceptions=True)
        self._auto_stop_task = None
        if not target_id:
            pending = self.pending_task_id
            if pending and self.pending:
                self.pending.cancel()
                await asyncio.gather(self.pending, return_exceptions=True)
                self.pending_task_id = ""
                return {"ok": True, "cancelled": True, "task_id": pending}
            raise ApiError(409, "RECORDING_NOT_ACTIVE", "当前没有本控制台启动的录屏")
        task = self._task(target_id)
        root = Path(task["task_root"])
        result = await self.gateway.call(
            ["record", "stop", "--task-root", str(root)],
            timeout=120,
            task_id=target_id,
        )
        self.active_task_id = ""
        self.active_started_at = 0
        if result.code != 0:
            raise ApiError(
                500,
                "RECORD_STOP_FAILED",
                result.err.strip() or "录屏停止失败",
                detail={"stdout": result.out, "stderr": result.err},
            )
        await self.reconciler.run()
        self.repo.add_event("record.stopped", "录屏已停止并完成校验", actor="user", task_id=target_id)
        self.bus.publish("record.updated", {"task_id": target_id, "recording": False})
        return {"ok": True, "task_id": target_id, "recording": result.data or {}}

    async def _auto_stop(self, task_id: str) -> None:
        await asyncio.sleep(self.settings.max_seconds + 1)
        if self.active_task_id != task_id:
            return
        try:
            self._auto_stop_task = None
            await self.stop(task_id)
            self.bus.publish("toast", {"level": "info", "message": "录屏达到 90 秒上限，已自动停止", "task_id": task_id})
        except Exception as exc:
            self.active_task_id = ""
            self.active_started_at = 0
            self.repo.add_event("record.failed", str(exc), level="error", actor="gateway", task_id=task_id)

    async def verify(self, task_id: str) -> dict[str, Any]:
        task = self._task(task_id)
        root = Path(task["task_root"])
        envelope = load_state(root)
        rec = envelope.state.recording if envelope.state and envelope.state.recording else None
        capture = _read_json(root / "recording" / "运行录屏-window-capture.json")
        cursor = _read_json(root / "recording" / "运行录屏-cursor-guard.json")
        errors: list[str] = []
        if rec is None or not rec.video or not Path(rec.video).is_file():
            errors.append("录屏文件不存在")
            return {
                "ok": False,
                "width": 0,
                "height": 0,
                "duration_s": 0,
                "capture_kind": "",
                "cursor_ok": False,
                "errors": errors,
            }
        if (rec.width, rec.height) != (1280, 720):
            errors.append(f"分辨率 {rec.width}×{rec.height}，要求 1280×720")
        if rec.durationSeconds > 90:
            errors.append(f"时长 {rec.durationSeconds:g} 秒，超过 90 秒")
        if rec.windowCaptureOk is not True:
            errors.append("窗口采集证据未通过")
        normal = capture.get("status") == "ok" and cursor.get("status") == "ok"
        manual = capture.get("status") == cursor.get("status") == "manual-import"
        if not (normal or manual):
            errors.append(f"capture={capture.get('status')} cursor={cursor.get('status')}")
        return {
            "ok": not errors,
            "width": rec.width,
            "height": rec.height,
            "duration_s": rec.durationSeconds,
            "capture_kind": rec.captureKind,
            "cursor_ok": cursor.get("pointerStrategy") == "none",
            "errors": errors,
        }

    async def delete(self, task_id: str, *, confirm: bool) -> dict[str, Any]:
        task = self._task(task_id)
        if not confirm:
            raise ApiError(422, "CONFIRM_REQUIRED", "删除录屏前必须二次确认")
        if self.active_task_id == task_id or self.pending_task_id == task_id:
            raise ApiError(409, "RECORDING_ACTIVE", "录屏进行中，不能删除")
        root = Path(task["task_root"])
        removed: list[str] = []
        for path in (
            root / "recording" / "运行录屏.mp4",
            root / "recording" / "运行录屏-window-capture.json",
            root / "recording" / "运行录屏-cursor-guard.json",
        ):
            if path.is_file():
                path.unlink()
                removed.append(str(path))
        self.repo.add_event(
            "record.deleted", "不合格录屏已删除，可重新录制", level="warn", actor="user", task_id=task_id
        )
        await self.reconciler.run()
        return {"ok": True, "removed": removed}

    async def open_terminal(self, task_id: str) -> dict[str, Any]:
        """让技能新开一个不带本机账号、主机名的 Terminal 窗口，专供录屏。"""
        task = self._task(task_id)
        result = await self.gateway.call(
            ["record", "terminal", "--task-root", task["task_root"]],
            timeout=60,
            task_id=task_id,
        )
        if result.code != 0:
            raise ApiError(
                500,
                "RECORD_TERMINAL_FAILED",
                result.err.strip() or "录屏终端打开失败",
                detail={"stdout": result.out, "stderr": result.err},
            )
        data = result.data or {}
        self.repo.add_event("record.terminal", "已打开干净的录屏终端", actor="user", task_id=task_id)
        return {"ok": True, "window_id": int(data.get("windowId") or 0), "title": str(data.get("title") or "")}

    async def chrome_start(self, task_id: str, url: str) -> dict[str, Any]:
        task = self._task(task_id)
        result = await self.gateway.call(
            ["record", "chrome-start", "--task-root", task["task_root"], "--url", url],
            timeout=120,
            task_id=task_id,
        )
        if result.code != 0:
            raise ApiError(500, "CHROME_START_FAILED", result.err.strip() or "独立 Chrome 启动失败")
        return result.data or {}

    async def chrome_stop(self, task_id: str) -> dict[str, Any]:
        task = self._task(task_id)
        result = await self.gateway.call(
            ["record", "chrome-stop", "--task-root", task["task_root"]],
            timeout=120,
            task_id=task_id,
        )
        if result.code != 0:
            raise ApiError(500, "CHROME_STOP_FAILED", result.err.strip() or "独立 Chrome 关闭失败")
        return result.data or {}

    def _task(self, task_id: str) -> dict[str, Any]:
        task = self.repo.get_task(task_id)
        if not task:
            raise ApiError(404, "TASK_NOT_FOUND", f"任务不存在：{task_id}")
        return task


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _task_id_for_root(repo: Repository, root: str) -> str:
    if not root:
        return ""
    task = repo.get_task_by_root(Path(root))
    return str(task["id"]) if task else ""


def parse_window_list(text: str) -> list[dict[str, Any]]:
    windows: list[dict[str, Any]] = []
    for line in text.splitlines():
        match = WINDOW_LINE.match(line)
        if not match:
            continue
        windows.append(
            {
                "id": int(match.group(1)),
                "windowId": int(match.group(1)),
                "app": match.group(2).strip(),
                "title": match.group(5).strip(),
                "width": int(match.group(3)),
                "height": int(match.group(4)),
                "owner_pid": 0,
            }
        )
    return windows
