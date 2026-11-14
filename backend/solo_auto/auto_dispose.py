"""Dispose of failed tasks without asking: clean up, return the claim, let intake refill.

A quarantined task used to wait for a human decision.  Nothing useful comes of
that wait, so the platform now removes the task's working data itself (to the
trash by default), returns the engineering claim and closes the queue item;
continuous refill then claims a fresh task at the back of the queue.

Left alone on purpose: items a human quarantined or marked "keep", and tasks
that were already exported or uploaded to SOLO2.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from solo_auto.dispatch.queue import DispatchItem

if TYPE_CHECKING:
    from solo_auto.context import AppContext

LOGGER = logging.getLogger(__name__)
MANUAL_QUARANTINE_TEXT = "人工隔离"
ACTOR = "dispatch"
LOOP_SECONDS = 20.0


class AutoDisposer:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self._recent: deque[float] = deque()
        self._throttled_at = 0.0
        self._skipped: dict[str, str] = {}

    async def loop(self) -> None:
        while True:
            try:
                await self.sweep()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("auto dispose sweep failed")
            await asyncio.sleep(LOOP_SECONDS)

    async def sweep(self) -> list[dict[str, Any]]:
        """Dispose of every quarantined item that is due; return what happened."""
        from solo_auto.api.purge import execute_purge, plan_purge

        ctx = self.ctx
        dispatcher = ctx.dispatcher
        config = dispatcher.config.dispose
        if not config.enabled or not dispatcher.v3_enabled:
            return []
        results: list[dict[str, Any]] = []
        for item in dispatcher.queue.all_items():
            if item.status != "quarantined":
                self._skipped.pop(item.task_id, None)
                continue
            reason = self._skip_reason(item, grace_seconds=config.grace_seconds)
            task = ctx.repo.get_task(item.task_id)
            if not reason and task is not None:
                reason = self._delivered_reason(task)
            if reason:
                self._note_skip(item, reason)
                continue
            if task is None:
                # 任务已经不在平台上，只剩队列项：直接关闭。
                self._close(item, "任务已不存在，自动关闭队列项")
                results.append({"task_id": item.task_id, "removed": True, "moved_to": ""})
                continue
            if not self._within_rate(config.max_per_hour):
                break
            plan = plan_purge(ctx, task)
            if plan["blockers"]:
                self._note_skip(item, "；".join(plan["blockers"]))
                continue
            failure = str((item.last_failure or {}).get("kind") or "") or item.wait_text
            try:
                outcome = await execute_purge(ctx, plan, permanent=config.permanent, actor=ACTOR)
            except Exception as error:
                message = getattr(error, "message", "") or f"{type(error).__name__}: {error}"
                self._note_skip(item, f"自动清理失败：{message}")
                continue
            self._recent.append(time.monotonic())
            self._close(item, "失败后已自动清理，由后续题目补位")
            ctx.repo.add_event(
                "task.auto_disposed",
                f"{plan['task_name']} 失败后已自动清理并归还工程，由后续题目补位"
                + (f"（失败原因：{failure[:200]}）" if failure else ""),
                level="warn",
                actor=ACTOR,
                details={
                    "task_id": item.task_id,
                    "failure": item.last_failure,
                    "moved_to": outcome.get("moved_to"),
                    "release_error": outcome.get("release_error"),
                    "permanent": config.permanent,
                },
            )
            results.append({"task_id": item.task_id, **outcome})
        if results:
            ctx.bus.publish("task.updated", {"purged": [row["task_id"] for row in results]})
            ctx.bus.publish("intake.updated", {"reason": "auto_disposed"})
        return results

    # ---- decisions ---------------------------------------------------

    def _skip_reason(self, item: DispatchItem, *, grace_seconds: int) -> str:
        if item.keep:
            return "已标记保留"
        if item.wait_text == MANUAL_QUARANTINE_TEXT:
            return "人工隔离的题目不自动清理"
        job = self.ctx.autopilot.jobs.get(item.task_id)
        if job is not None and job.active:
            return "自动交付仍在进行"
        age = _age_seconds(item.updated_at)
        if age is not None and age < grace_seconds:
            return f"等待 {grace_seconds} 秒观察期"
        return ""

    def _delivered_reason(self, task: dict[str, Any]) -> str:
        if task.get("exported_at"):
            return "已导出交付表，保留现场"
        root = Path(str(task.get("task_root") or ""))
        try:
            data = json.loads((root / "monitor" / "submission" / "api-result.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return ""
        if isinstance(data, dict) and data.get("submissionId") not in (None, ""):
            return "已提交到 SOLO2，保留现场"
        return ""

    def _within_rate(self, limit: int) -> bool:
        now = time.monotonic()
        while self._recent and now - self._recent[0] > 3600:
            self._recent.popleft()
        if len(self._recent) < limit:
            return True
        if now - self._throttled_at > 3600 or not self._throttled_at:
            self._throttled_at = now
            text = f"一小时内已自动清理 {len(self._recent)} 道失败题，达到上限 {limit}，暂停自动清理；请检查是否存在环境故障"
            self.ctx.repo.add_event("task.auto_dispose_throttled", text, level="error", actor=ACTOR)
            self.ctx.notifier.notify(title="solo-auto 失败题自动清理已暂停", status=f"一小时内 {len(self._recent)} 道")
        return False

    def _note_skip(self, item: DispatchItem, reason: str) -> None:
        if self._skipped.get(item.task_id) == reason or reason.startswith("等待 "):
            return
        self._skipped[item.task_id] = reason
        self.ctx.repo.add_event(
            "task.auto_dispose_skipped",
            f"{item.task_id} 未自动清理：{reason}",
            actor=ACTOR,
            task_id=item.task_id,
        )

    def _close(self, item: DispatchItem, text: str) -> None:
        queue = self.ctx.dispatcher.queue
        queue.remove(item.task_id)
        queue.update(item.task_id, wait_code="auto_disposed", wait_text=text)
        self._skipped.pop(item.task_id, None)


def _age_seconds(value: str) -> float | None:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return (datetime.now(parsed.tzinfo) - parsed).total_seconds()
