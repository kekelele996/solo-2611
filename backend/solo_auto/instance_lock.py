"""Single-process guard using a non-blocking file lock."""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
from types import TracebackType
from typing import TextIO


class AlreadyRunningError(RuntimeError):
    pass


class InstanceLock:
    def __init__(self, lock_path: Path, pid_path: Path) -> None:
        self.lock_path = lock_path
        self.pid_path = pid_path
        self._handle: TextIO | None = None

    def acquire(self) -> None:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.lock_path.open("a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise AlreadyRunningError(f"已有 solo-auto 实例运行：{self.pid_path}") from exc
        self._handle = handle
        self.pid_path.write_text(str(os.getpid()), encoding="utf-8")

    def release(self) -> None:
        if self._handle is None:
            return
        handle = self._handle
        self.pid_path.unlink(missing_ok=True)
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
        self._handle = None

    def __enter__(self) -> InstanceLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()
