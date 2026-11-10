"""User-triggered local verification commands."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
from datetime import datetime
from pathlib import Path
from typing import Any

from solo_auto.errors import ApiError
from solo_auto.events.bus import EventBus
from solo_auto.store.repo import Repository

MAX_OUTPUT = 64 * 1024


async def run_verification(
    *,
    task_id: str,
    task_root: Path,
    command: str,
    repo: Repository,
    bus: EventBus,
) -> dict[str, Any]:
    command = command.strip()
    if not command:
        raise ApiError(422, "VERIFY_COMMAND_REQUIRED", "请填写要执行的复核命令")
    workspace = task_root / "workspace"
    if not workspace.is_dir():
        raise ApiError(409, "WORKSPACE_MISSING", "workspace 不存在")
    started = datetime.now().astimezone()
    shell = shutil.which("zsh") or shutil.which("sh") or "/bin/sh"
    proc = await asyncio.create_subprocess_exec(
        shell,
        "-lc",
        command,
        cwd=str(workspace),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    timed_out = False
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=300)
    except TimeoutError:
        timed_out = True
        if proc.pid:
            os.killpg(proc.pid, signal.SIGTERM)
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=5)
        except TimeoutError:
            if proc.pid:
                os.killpg(proc.pid, signal.SIGKILL)
            stdout_b, stderr_b = await proc.communicate()
    stdout = stdout_b.decode("utf-8", errors="replace")
    stderr = stderr_b.decode("utf-8", errors="replace")
    payload = {
        "schemaVersion": 1,
        "cmd": command,
        "cwd": str(workspace),
        "exitCode": proc.returncode,
        "timedOut": timed_out,
        "startedAt": started.isoformat(),
        "finishedAt": datetime.now().astimezone().replace(microsecond=0).isoformat(),
        "stdout": stdout[:MAX_OUTPUT],
        "stderr": stderr[:MAX_OUTPUT],
        "truncated": len(stdout) > MAX_OUTPUT or len(stderr) > MAX_OUTPUT,
    }
    path = task_root / "monitor" / "runtime-verification.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    level = "info" if payload["exitCode"] == 0 and not timed_out else "warn"
    repo.add_event(
        "verify.completed",
        f"本机复核退出码 {payload['exitCode']}" + ("（超时）" if timed_out else ""),
        level=level,
        actor="user",
        task_id=task_id,
        details={"cmd": command, "exitCode": payload["exitCode"], "timedOut": timed_out},
    )
    bus.publish("task.updated", {"task_id": task_id, "action": "verify.completed"})
    return payload
