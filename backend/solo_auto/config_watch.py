"""Hot-reload config.toml so edits take effect while tasks keep running.

Every component reads its limits from the live settings objects on each
scheduling pass, so installing a new settings object is enough for the next
pass to obey it. Raising a limit starts more work at the usual launch spacing;
lowering one only stops new launches, so the pool drains to the new limit as
running tasks finish. Nothing that is already running is interrupted.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from solo_auto.settings import Settings, load_settings

if TYPE_CHECKING:
    from solo_auto.context import AppContext

LOGGER = logging.getLogger(__name__)

# 这些配置段只在启动时读取（监听地址、目录、外部账号等），改了需要重启服务。
RESTART_FIELDS = (
    "host",
    "port",
    "skill_root",
    "tasks_dir",
    "source",
    "solo2",
    "execution",
    "storage",
    "recording",
    "submission",
)


def _dump(value: Any, *, exclude: set[str] | None = None) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", exclude=exclude)
    return value


class ConfigWatcher:
    def __init__(self, ctx: AppContext, *, interval: float = 5.0) -> None:
        self.ctx = ctx
        self.interval = interval
        self._signature = self._stat()
        self._task: asyncio.Task[None] | None = None

    def _stat(self) -> tuple[int, int] | None:
        try:
            stat = self.ctx.paths.config.stat()
        except OSError:
            return None
        return stat.st_mtime_ns, stat.st_size

    def start(self) -> None:
        if self._task is None:
            self._signature = self._stat()
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            try:
                await self.check()
            except Exception:
                LOGGER.exception("config hot reload failed")

    async def check(self) -> dict[str, Any]:
        signature = self._stat()
        if signature is None or signature == self._signature:
            return {"applied": [], "restart_required": []}
        self._signature = signature
        try:
            fresh = load_settings(self.ctx.paths)
        except Exception as exc:  # TOML 语法错误或字段校验失败：保留当前配置
            self.ctx.repo.add_event(
                "config.reload_failed",
                f"config.toml 解析失败，继续使用当前配置：{str(exc)[:300]}",
                level="warn",
                actor="config",
            )
            return {"applied": [], "restart_required": [], "error": str(exc)}
        return await self.apply(fresh)

    async def apply(self, fresh: Settings) -> dict[str, Any]:
        ctx = self.ctx
        current = ctx.settings
        applied: list[str] = []
        restart = [name for name in RESTART_FIELDS if _dump(getattr(current, name)) != _dump(getattr(fresh, name))]

        if fresh._dispatch_validation_error:
            # load_settings 会把非法的 [dispatch] 回退成默认值；热更新时绝不能把默认值当成新配置。
            ctx.repo.add_event(
                "config.reload_failed",
                f"[dispatch] 校验失败，调度配置保持不变：{fresh._dispatch_validation_error[:300]}",
                level="warn",
                actor="config",
            )
        elif _dump(current.dispatch) != _dump(fresh.dispatch):
            result = await ctx.dispatcher.apply_config(fresh.dispatch, actor="config")
            if result.get("changed"):
                applied.append("dispatch")

        if _dump(current.autopilot) != _dump(fresh.autopilot):
            current.autopilot = fresh.autopilot
            ctx.autopilot.apply_settings(fresh.autopilot)
            applied.append("autopilot")
        if _dump(current.audit) != _dump(fresh.audit):
            current.audit = fresh.audit
            ctx.audits.settings = fresh.audit
            applied.append("audit")
        if _dump(current.intake) != _dump(fresh.intake):
            current.intake = fresh.intake
            ctx.intake.intake_settings = fresh.intake
            applied.append("intake")
        if _dump(current.trash) != _dump(fresh.trash):
            current.trash = fresh.trash
            ctx.trash.settings = fresh.trash
            applied.append("trash")
        if _dump(current.slim) != _dump(fresh.slim):
            current.slim = fresh.slim
            ctx.janitor.settings = fresh.slim
            applied.append("slim")
        if _dump(current.notify, exclude={"webhook_url"}) != _dump(fresh.notify, exclude={"webhook_url"}):
            fresh.notify.webhook_url = current.notify.webhook_url  # 密钥来自钥匙串，不在文件里
            current.notify = fresh.notify
            ctx.notifier.settings = fresh.notify
            applied.append("notify")

        if applied:
            ctx.repo.add_event(
                "config.reloaded",
                "config.toml 已热更新：" + "、".join(applied),
                actor="config",
                details={"applied": applied},
            )
        if restart:
            ctx.repo.add_event(
                "config.restart_required",
                "以下配置只在启动时读取，重启服务后生效：" + "、".join(restart),
                level="warn",
                actor="config",
                details={"fields": restart},
            )
        return {"applied": applied, "restart_required": restart}
