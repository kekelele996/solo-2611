"""Background retention and conservative workspace slimming."""

from __future__ import annotations

import asyncio
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from solo_auto.events.bus import EventBus
from solo_auto.gateway.skill import SkillGateway
from solo_auto.pipeline.reconcile import Reconciler
from solo_auto.pipeline.trash import TrashService
from solo_auto.settings import Paths, SlimSettings
from solo_auto.store.repo import Repository
from solo_auto.store.state import load_state


class Janitor:
    def __init__(
        self,
        *,
        paths: Paths,
        repo: Repository,
        reconciler: Reconciler,
        trash: TrashService,
        settings: SlimSettings,
        bus: EventBus,
        gateway: SkillGateway,
    ) -> None:
        self.paths = paths
        self.repo = repo
        self.reconciler = reconciler
        self.trash = trash
        self.settings = settings
        self.bus = bus
        self.gateway = gateway
        self._task: asyncio.Task[None] | None = None
        self._terminal_task: asyncio.Task[None] | None = None
        self._terminal_error = ""

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())
        if self._terminal_task is None:
            self._terminal_task = asyncio.create_task(self._terminal_loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        if self._terminal_task is not None:
            self._terminal_task.cancel()
            await asyncio.gather(self._terminal_task, return_exceptions=True)
            self._terminal_task = None

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.tick)
            except Exception as exc:
                self.repo.add_event(
                    "janitor.failed",
                    f"{type(exc).__name__}: {exc}",
                    level="error",
                    actor="janitor",
                )
            await asyncio.sleep(3600)

    async def _terminal_loop(self) -> None:
        """Reap windows left by an older skill or a killed supervisor."""
        while True:
            try:
                result = await self.gateway.call(["terminal-sweep"], timeout=30)
                data = result.data or {}
                if result.code != 0:
                    error = result.err.strip() or f"terminal-sweep exit={result.code}"
                    if error != self._terminal_error:
                        self._terminal_error = error
                        self.repo.add_event(
                            "terminal.sweep_failed",
                            error[:500],
                            level="warn",
                            actor="janitor",
                        )
                elif data.get("closed"):
                    self._terminal_error = ""
                    self.repo.add_event(
                        "terminal.swept",
                        f"已清理 {int(data.get('closed') or 0)} 个已结束会话的残留终端窗口",
                        actor="janitor",
                        details=data,
                    )
                else:
                    self._terminal_error = ""
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                if error != self._terminal_error:
                    self._terminal_error = error
                    self.repo.add_event(
                        "terminal.sweep_failed",
                        error[:500],
                        level="warn",
                        actor="janitor",
                    )
            await asyncio.sleep(60)

    def tick(self) -> dict[str, Any]:
        return {
            "trash_removed": self.trash.purge_expired(),
            "slimmed": self.slim_exported(),
        }

    def slim_exported(self) -> list[dict[str, Any]]:
        if not self.settings.enabled:
            return []
        cutoff = datetime.now().astimezone() - timedelta(days=self.settings.after_days)
        removed: list[dict[str, Any]] = []
        for task in self.repo.query("SELECT * FROM tasks WHERE exported_at IS NOT NULL"):
            try:
                exported = datetime.fromisoformat(str(task.get("exported_at") or ""))
            except ValueError:
                continue
            if exported > cutoff:
                continue
            envelope = load_state(Path(str(task["task_root"])))
            if envelope.state is None:
                continue
            if not envelope.state.remote.artifactPushed:
                self.repo.add_event(
                    "task.slim_skipped",
                    "产物快照尚未确认推送，未执行瘦身",
                    level="warn",
                    actor="janitor",
                    task_id=str(task["id"]),
                )
                continue
            workspace = Path(envelope.state.workspace)
            if not workspace.is_dir():
                continue
            status = _git(workspace, ["status", "--porcelain"])
            if status.returncode != 0 or status.stdout.strip():
                self.repo.add_event(
                    "task.slim_skipped",
                    "workspace 不是干净的 Git 工作区，未执行瘦身",
                    level="warn",
                    actor="janitor",
                    task_id=str(task["id"]),
                )
                continue
            matched = self._matched_directories(workspace)
            if not matched:
                continue
            size = sum(_size(path) for path in matched)
            for path in matched:
                shutil.rmtree(path, ignore_errors=True)
            item = {
                "task_id": task["id"],
                "task_name": task["task_name"],
                "paths": [str(path) for path in matched],
                "bytes": size,
            }
            removed.append(item)
            self.repo.add_event(
                "task.slimmed",
                f"已删除 {len(matched)} 个依赖或构建目录，释放 {size / 1024**2:.1f} MB",
                actor="janitor",
                task_id=str(task["id"]),
                details={"paths": item["paths"], "bytes": size},
            )
        return removed

    def _matched_directories(self, workspace: Path) -> list[Path]:
        matched: list[Path] = []
        for pattern in self.settings.patterns:
            for path in workspace.rglob(pattern):
                try:
                    if path.is_dir() and not path.is_symlink() and path.is_relative_to(workspace):
                        matched.append(path)
                except OSError:
                    continue
        # 删父目录后，子目录不必再单独处理
        result: list[Path] = []
        for path in sorted(set(matched), key=lambda item: len(item.parts)):
            if not any(path.is_relative_to(parent) for parent in result):
                result.append(path)
        return result


def _git(workspace: Path, argv: list[str]) -> Any:
    import subprocess

    return subprocess.run(["git", "-C", str(workspace), *argv], capture_output=True, text=True, check=False)


def _size(path: Path) -> int:
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file() and not item.is_symlink():
                total += item.lstat().st_size
        except OSError:
            continue
    return total
