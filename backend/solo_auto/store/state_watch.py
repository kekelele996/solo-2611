"""Debounced watchdog integration for skill state files."""

from __future__ import annotations

import asyncio
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer


class _Handler(FileSystemEventHandler):
    def __init__(self, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue[str]) -> None:
        self.loop = loop
        self.queue = queue

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        if Path(str(event.src_path)).name == "state.json":
            self.loop.call_soon_threadsafe(self.queue.put_nowait, str(event.src_path))


class StateWatcher:
    def __init__(self, tasks_dir: Path) -> None:
        self.tasks_dir = tasks_dir
        self.observer = Observer()
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self._last_fingerprint = ""

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        self.observer.schedule(_Handler(loop, self.queue), str(self.tasks_dir), recursive=True)
        self.observer.start()

    def stop(self) -> None:
        self.observer.stop()
        self.observer.join(timeout=3)

    async def changes(self, on_debounced: object) -> None:
        callback = on_debounced
        while True:
            path = await self.queue.get()
            seen = {path}
            deadline = asyncio.get_running_loop().time() + 0.1
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                try:
                    seen.add(await asyncio.wait_for(self.queue.get(), timeout=remaining))
                except TimeoutError:
                    break
            fingerprint_parts: list[str] = []
            for item in sorted(seen):
                candidate = Path(item)
                try:
                    stat = candidate.stat()
                    fingerprint_parts.append(f"{candidate}:{stat.st_mtime_ns}:{stat.st_size}")
                except OSError:
                    fingerprint_parts.append(f"{candidate}:missing")
            fingerprint = "|".join(fingerprint_parts)
            if fingerprint == self._last_fingerprint:
                continue
            self._last_fingerprint = fingerprint
            if callable(callback):
                result = callback()
                if asyncio.iscoroutine(result):
                    await result
