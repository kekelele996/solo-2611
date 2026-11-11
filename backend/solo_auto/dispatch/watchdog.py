"""Loop heartbeat and stall detection."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass


@dataclass(slots=True)
class LoopStatus:
    name: str
    interval: float
    stall_after: float
    last_started: float = 0.0
    last_finished: float = 0.0
    status: str = "idle"
    errors: int = 0
    traceback: str = ""

    def view(self) -> dict[str, object]:
        return {
            "name": self.name,
            "status": self.status,
            "last_beat_at": self.last_finished,
            "last_started_at": self.last_started,
            "errors": self.errors,
            "traceback": self.traceback,
        }


class LoopHealth:
    def __init__(self) -> None:
        self.loops: dict[str, LoopStatus] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def register(self, name: str, *, interval: float, stall_after: float | None = None) -> None:
        self.loops[name] = LoopStatus(
            name=name,
            interval=interval,
            stall_after=stall_after or max(60, interval * 10),
        )

    @asynccontextmanager
    async def beat(self, name: str) -> AsyncIterator[None]:
        loop = self.loops[name]
        loop.last_started = time.monotonic()
        loop.status = "running"
        try:
            yield
        except Exception:
            loop.errors += 1
            loop.status = "error"
            raise
        else:
            loop.errors = 0
            loop.status = "ok"
        finally:
            loop.last_finished = time.monotonic()

    def scan(self, now: float | None = None) -> list[LoopStatus]:
        current = now or time.monotonic()
        stalled: list[LoopStatus] = []
        for loop in self.loops.values():
            task = self._tasks.get(loop.name)
            if task is not None and task.done() and loop.status == "running":
                loop.status = "dead"
            last = loop.last_finished or loop.last_started
            if loop.status == "running" and last and current - last > loop.stall_after:
                loop.status = "stalled"
                stalled.append(loop)
        return stalled

    @property
    def ok(self) -> bool:
        self.scan()
        return not any(loop.status in {"stalled", "dead"} for loop in self.loops.values())

    def snapshot(self) -> list[dict[str, object]]:
        return [loop.view() for loop in self.loops.values()]

    def track(self, name: str, task: asyncio.Task[None]) -> None:
        self._tasks[name] = task
