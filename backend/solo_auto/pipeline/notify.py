"""Best-effort local and webhook notifications with privacy-safe payloads."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import sys
from typing import Any

import httpx

from solo_auto.settings import NotifySettings

LOGGER = logging.getLogger(__name__)


class Notifier:
    def __init__(self, settings: NotifySettings) -> None:
        self.settings = settings
        self._tasks: set[asyncio.Task[None]] = set()

    def notify(self, *, title: str, status: str, task_name: str = "") -> None:
        if not self.settings.macos and not self.settings.webhook_url:
            return
        task = asyncio.create_task(self._send(title=title, status=status, task_name=task_name))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _send(self, *, title: str, status: str, task_name: str) -> None:
        plain = f"{task_name}：{status}" if task_name else status
        if self.settings.macos and sys.platform == "darwin" and shutil.which("osascript"):
            script = (
                "on run argv\n"
                "display notification (item 1 of argv) with title (item 2 of argv)\n"
                "end run"
            )
            proc = await asyncio.create_subprocess_exec(
                "osascript",
                "-e",
                script,
                plain,
                title,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
        if self.settings.webhook_url:
            payload: dict[str, Any] = {"title": title, "status": status, "task_name": task_name}
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    response = await client.post(self.settings.webhook_url, json=payload)
                    response.raise_for_status()
            except Exception:
                LOGGER.warning("webhook notification failed", exc_info=True)

    async def close(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)


def masked_webhook(settings: NotifySettings) -> str:
    return json.dumps({"configured": bool(settings.webhook_url), "macos": settings.macos})
