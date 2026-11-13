"""Terminate skill processes a ChatGPT session left behind for a task.

Stopping a ChatGPT turn does not stop the commands it started. A leftover
``solo2611.py run --task-root <root>`` keeps retrying and starts a fresh
container after the platform's cleanup removed the old one, taking capacity
away from tasks that are really running. Before any cleanup we therefore end
every skill process that names the task root, together with its children.

Processes started by this service (scheduler runs, gateway calls) are left
alone: the scheduler stops its own runs, and the cleanup call that follows is
itself one of our children.
"""

from __future__ import annotations

import asyncio
import os
import re
import signal
import subprocess
import sys
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SKILL_MARKERS = ("solo2611",)


@dataclass(frozen=True, slots=True)
class ProcessRow:
    pid: int
    ppid: int
    command: str


def list_processes() -> list[ProcessRow]:
    if sys.platform == "win32":
        return []
    try:
        result = subprocess.run(
            ["ps", "-axww", "-o", "pid=,ppid=,command="],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    rows: list[ProcessRow] = []
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        rows.append(ProcessRow(int(parts[0]), int(parts[1]), parts[2]))
    return rows


def _root_pattern(task_root: Path) -> re.Pattern[str]:
    spellings = {str(task_root)}
    with suppress(OSError):
        spellings.add(str(task_root.resolve()))
    # 路径后面必须是分隔符或结尾，避免 `xxx-ab` 误匹配 `xxx-abc`。
    alternatives = "|".join(re.escape(item.rstrip("/")) for item in sorted(spellings, key=len, reverse=True))
    return re.compile(rf"(?:{alternatives})(?=$|[/\s\"'])")


def _descendants(rows: list[ProcessRow], roots: set[int]) -> set[int]:
    children: dict[int, list[int]] = {}
    for row in rows:
        children.setdefault(row.ppid, []).append(row.pid)
    found: set[int] = set()
    stack = list(roots)
    while stack:
        pid = stack.pop()
        for child in children.get(pid, []):
            if child not in found:
                found.add(child)
                stack.append(child)
    return found


def _ancestors(rows: list[ProcessRow], pid: int) -> set[int]:
    parents = {row.pid: row.ppid for row in rows}
    found: set[int] = set()
    while pid and pid not in found:
        found.add(pid)
        pid = parents.get(pid, 0)
    return found


def find_task_processes(task_root: Path, rows: list[ProcessRow] | None = None) -> list[ProcessRow]:
    """Skill processes outside this service that work on ``task_root``, plus their children."""
    rows = list_processes() if rows is None else rows
    me = os.getpid()
    protected = _ancestors(rows, me) | _descendants(rows, {me}) | {me}
    pattern = _root_pattern(task_root)
    seeds = {
        row.pid
        for row in rows
        if row.pid not in protected
        and any(marker in row.command for marker in SKILL_MARKERS)
        and pattern.search(row.command)
    }
    if not seeds:
        return []
    targets = (seeds | _descendants(rows, seeds)) - protected
    return [row for row in rows if row.pid in targets]


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def reap_task_processes(task_root: Path, *, grace_seconds: float = 5.0) -> list[dict[str, object]]:
    """SIGTERM the task's leftover processes, then SIGKILL whatever survives the grace period."""
    targets = find_task_processes(task_root)
    if not targets:
        return []
    for row in targets:
        try:
            os.kill(row.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            continue
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline and any(_alive(row.pid) for row in targets):
        time.sleep(0.1)
    killed: set[int] = set()
    for row in targets:
        if _alive(row.pid):
            try:
                os.kill(row.pid, signal.SIGKILL)
                killed.add(row.pid)
            except (ProcessLookupError, PermissionError):
                continue
    return [
        {"pid": row.pid, "command": row.command[:300], "signal": "KILL" if row.pid in killed else "TERM"}
        for row in targets
    ]


async def reap_task(task_root: Path | str, *, grace_seconds: float = 5.0) -> list[dict[str, object]]:
    return await asyncio.to_thread(reap_task_processes, Path(task_root), grace_seconds=grace_seconds)


def reaped_note(reaped: list[dict[str, object]]) -> str:
    return f"；已结束 {len(reaped)} 个遗留进程" if reaped else ""


async def reap_and_record(
    repo: Any,
    task_root: Path | str,
    *,
    task_id: str | None,
    actor: str,
) -> list[dict[str, object]]:
    """Reap leftover processes and leave an event so the user can see what was ended."""
    reaped = await reap_task(task_root)
    if reaped:
        repo.add_event(
            "task.processes_reaped",
            f"已结束 {len(reaped)} 个遗留进程（ChatGPT 会话停止后仍在运行的技能命令）",
            level="warn",
            actor=actor,
            task_id=task_id,
            details={"processes": reaped},
        )
    return reaped
