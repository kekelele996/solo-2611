"""Async, whitelisted access to the existing solo-2611 CLI."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import re
import signal
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from solo_auto.errors import SkillInvocationError
from solo_auto.logging_setup import redact

LOGGER = logging.getLogger(__name__)

COMMANDS = {
    "version",
    "config",
    "init",
    "prompt",
    "prompt-history",
    "run",
    "run-status",
    "resume",
    "continue",
    "abort",
    "resume-all",
    "doctor",
    "supervise",
    "interactive",
    "collect",
    "reset",
    "snapshot",
    "digest",
    "status",
    "record",
    "feedback",
    "export",
    "export-batch",
    "bundle",
    "repo",
    "cleanup",
    "terminal-sweep",
}


class SkillResult(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    code: int
    out: str = ""
    err: str = ""
    duration: float
    data: dict[str, Any] | None = None


class StreamEvent(BaseModel):
    kind: str
    line: str
    at: str


@dataclass
class StreamResult:
    result: SkillResult
    lines: list[StreamEvent] = field(default_factory=list)


class SkillGateway:
    METADATA_CACHE_TTL_SECONDS = 30.0
    CACHEABLE_CALLS = {
        ("version",),
        ("config", "show"),
    }

    def __init__(
        self,
        skill_root: Path,
        log_root: Path,
        *,
        python: str = "python3",
        metadata_cache_ttl_seconds: float = METADATA_CACHE_TTL_SECONDS,
    ) -> None:
        self.skill_root = skill_root.expanduser().resolve()
        self.script = self.skill_root / "scripts" / "solo2611.py"
        self.log_root = log_root
        self.python = python
        self.metadata_cache_ttl_seconds = max(0.0, metadata_cache_ttl_seconds)
        self._active_processes: dict[str, asyncio.subprocess.Process] = {}
        self._metadata_cache: dict[tuple[str, ...], tuple[float, SkillResult]] = {}
        self._metadata_locks: dict[tuple[str, ...], asyncio.Lock] = {}
        if not self.script.is_file():
            raise RuntimeError(f"solo-2611 技能脚本不存在: {self.script}")
        self.spec_path = self.skill_root / "scripts"

    def assert_command(self, argv: list[str]) -> None:
        if not argv or argv[0] not in COMMANDS:
            raise ValueError(f"技能命令不在白名单: {argv[:1]}")

    def clean_env(self) -> dict[str, str]:
        allowed = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT")
        env = {key: os.environ[key] for key in allowed if key in os.environ}
        env.setdefault("PATH", "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin")
        return env

    def _cmd(self, argv: list[str]) -> list[str]:
        self.assert_command(argv)
        return [self.python, "-I", str(self.script), *argv]

    async def call(
        self,
        argv: list[str],
        *,
        timeout: float = 300,
        task_id: str | None = None,
        cwd: Path | None = None,
    ) -> SkillResult:
        cache_key = tuple(argv)
        if cache_key in self.CACHEABLE_CALLS and self.metadata_cache_ttl_seconds > 0:
            cached = self._cached_metadata(cache_key)
            if cached is not None:
                return cached
            lock = self._metadata_locks.setdefault(cache_key, asyncio.Lock())
            async with lock:
                cached = self._cached_metadata(cache_key)
                if cached is not None:
                    return cached
                result = await self._call_uncached(argv, timeout=timeout, task_id=task_id, cwd=cwd)
                if result.code == 0:
                    self._metadata_cache[cache_key] = (time.monotonic(), result)
                return result

        result = await self._call_uncached(argv, timeout=timeout, task_id=task_id, cwd=cwd)
        config_changed = len(argv) >= 2 and argv[0] == "config" and argv[1] in {"set", "init"}
        if result.code == 0 and (config_changed or argv[:1] == ["init"]):
            self.invalidate_metadata()
        return result

    def invalidate_metadata(self) -> None:
        self._metadata_cache.clear()

    def _cached_metadata(self, cache_key: tuple[str, ...]) -> SkillResult | None:
        entry = self._metadata_cache.get(cache_key)
        if entry is None:
            return None
        cached_at, result = entry
        if time.monotonic() - cached_at >= self.metadata_cache_ttl_seconds:
            self._metadata_cache.pop(cache_key, None)
            return None
        cloned = result.model_copy(deep=True)
        cloned.data = copy.deepcopy(result.data)
        return cloned

    async def _call_uncached(
        self,
        argv: list[str],
        *,
        timeout: float,
        task_id: str | None,
        cwd: Path | None,
    ) -> SkillResult:
        command = self._cmd(argv)
        started = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(cwd or self.skill_root),
            env=self.clean_env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except TimeoutError:
                proc.kill()
                await proc.wait()
            stdout_b = b""
            stderr_b = "技能调用超时".encode()
        elapsed = round(time.monotonic() - started, 3)
        stdout = stdout_b.decode("utf-8", errors="replace")
        stderr = stderr_b.decode("utf-8", errors="replace")
        code = proc.returncode if proc.returncode is not None else 1
        data = self._parse_json(stdout) if code in {0, 2} else None
        self._write_log(argv, code, elapsed, stdout, stderr, task_id)
        return SkillResult(code=code, out=stdout, err=stderr, duration=elapsed, data=data)

    async def require(
        self,
        argv: list[str],
        *,
        timeout: float = 300,
        task_id: str | None = None,
    ) -> SkillResult:
        result = await self.call(argv, timeout=timeout, task_id=task_id)
        if result.code == 1:
            message = result.err.strip() or result.out.strip() or f"技能调用失败 ({result.code})"
            raise SkillInvocationError(message, code=1, stdout=result.out, stderr=result.err)
        return result

    async def stream(
        self,
        argv: list[str],
        *,
        task_root: Path | None,
        on_event: Callable[[StreamEvent], Awaitable[None]] | None = None,
        timeout: float | None = None,
        task_id: str | None = None,
        cancel_key: str | None = None,
    ) -> StreamResult:
        command = self._cmd(argv)
        started = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(self.skill_root),
            env=self.clean_env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        if proc.stdout is None or proc.stderr is None:
            proc.kill()
            await proc.wait()
            raise RuntimeError("技能子进程未建立 stdout/stderr 管道")
        if cancel_key:
            self._active_processes[cancel_key] = proc
        events: list[StreamEvent] = []
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        stop_tail = asyncio.Event()

        async def publish(kind: str, line: str) -> None:
            event = StreamEvent(kind=kind, line=redact(line), at=datetime.now().astimezone().isoformat())
            events.append(event)
            if on_event is not None:
                await on_event(event)

        async def read_stream(stream: asyncio.StreamReader, kind: str, parts: list[str]) -> None:
            while True:
                line_b = await stream.readline()
                if not line_b:
                    break
                line = line_b.decode("utf-8", errors="replace").rstrip()
                parts.append(line)
                await publish(kind, line)

        async def tail_attempt_files() -> None:
            if task_root is None:
                return
            state_path = task_root / "monitor" / "state.json"
            positions: dict[Path, int] = {}
            while True:
                try:
                    state = json.loads(state_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    state = {}
                attempts = state.get("attempts") if isinstance(state, dict) else []
                if isinstance(attempts, list) and attempts:
                    current = attempts[-1]
                    raw_dir = current.get("dir") if isinstance(current, dict) else None
                    if raw_dir:
                        directory = Path(str(raw_dir))
                        for name in ("stdout.jsonl", "stderr.log"):
                            path = directory / name
                            if not path.is_file():
                                continue
                            position = positions.get(path, 0)
                            with path.open("r", encoding="utf-8", errors="replace") as handle:
                                handle.seek(position)
                                chunk = handle.read(64 * 1024)
                                positions[path] = handle.tell()
                            for line in chunk.splitlines():
                                if line.strip():
                                    await publish("container", f"{name}: {line}")
                if stop_tail.is_set():
                    break
                with suppress(TimeoutError):
                    await asyncio.wait_for(stop_tail.wait(), timeout=0.2)

        readers = [
            asyncio.create_task(read_stream(proc.stdout, "stdout", stdout_parts)),
            asyncio.create_task(read_stream(proc.stderr, "stderr", stderr_parts)),
            asyncio.create_task(tail_attempt_files()),
        ]
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout)
        except TimeoutError:
            if proc.pid:
                os.killpg(proc.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except TimeoutError:
                if proc.pid:
                    os.killpg(proc.pid, signal.SIGKILL)
                await proc.wait()
            stderr_parts.append("技能流式调用超时")
        except asyncio.CancelledError:
            if proc.returncode is None and proc.pid:
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    await asyncio.wait_for(proc.wait(), timeout=10)
                except TimeoutError:
                    os.killpg(proc.pid, signal.SIGKILL)
                    await proc.wait()
            raise
        finally:
            stop_tail.set()
            await asyncio.gather(*readers, return_exceptions=True)
            if cancel_key:
                self._active_processes.pop(cancel_key, None)
        elapsed = round(time.monotonic() - started, 3)
        code = proc.returncode if proc.returncode is not None else 1
        stdout = "\n".join(stdout_parts)
        stderr = "\n".join(stderr_parts)
        data = self._parse_json(stdout) if code in {0, 2} else None
        self._write_log(argv, code, elapsed, stdout, stderr, task_id)
        return StreamResult(
            result=SkillResult(code=code, out=stdout, err=stderr, duration=elapsed, data=data),
            lines=events,
        )

    async def cancel(self, cancel_key: str) -> bool:
        proc = self._active_processes.get(cancel_key)
        if proc is None or proc.returncode is not None or not proc.pid:
            return False
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(proc.wait(), timeout=10)
        except TimeoutError:
            os.killpg(proc.pid, signal.SIGKILL)
            await proc.wait()
        return True

    async def version(self) -> str:
        result = await self.call(["version"], timeout=30)
        if result.code != 0:
            raise SkillInvocationError(
                result.err or "无法读取技能版本", code=result.code, stdout=result.out, stderr=result.err
            )
        return result.out.strip().splitlines()[-1]

    async def config(self) -> dict[str, Any]:
        result = await self.call(["config", "show"], timeout=30)
        if result.code != 0 or result.data is None:
            raise SkillInvocationError("无法读取技能配置", code=result.code, stdout=result.out, stderr=result.err)
        return result.data

    def _parse_json(self, stdout: str) -> dict[str, Any] | None:
        text = stdout.strip()
        if not text:
            return None
        try:
            parsed = json.loads(text)
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            if lines:
                try:
                    parsed = json.loads(lines[-1])
                    return parsed if isinstance(parsed, dict) else None
                except json.JSONDecodeError:
                    return None
            return None

    @staticmethod
    def classify_result(result: SkillResult) -> str:
        if result.code == 0:
            return "ok"
        if result.code == 2:
            return "gate"
        return "business"

    async def run_status(self, task_root: Path, *, task_id: str | None = None) -> SkillResult:
        return await self.call(
            ["run-status", "--task-root", str(task_root), "--json", "--lines", "40"],
            timeout=15,
            task_id=task_id,
        )

    def _write_log(
        self,
        argv: list[str],
        code: int,
        duration: float,
        stdout: str,
        stderr: str,
        task_id: str | None,
    ) -> None:
        day = datetime.now().strftime("%Y-%m-%d")
        directory = self.log_root / (task_id or "_system")
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%H%M%S-%f")
        name = re.sub(r"[^A-Za-z0-9_.-]+", "-", argv[0]) if argv else "unknown"
        path = directory / f"{day}-{name}-{stamp}.log"
        text = "\n".join(
            [
                f"argv={redact(argv)}",
                f"duration={duration:.3f}s",
                f"exit_code={code}",
                "--- stdout (first 4KB) ---",
                redact(stdout[:4096]),
                "--- stderr (first 4KB) ---",
                redact(stderr[:4096]),
                "",
            ]
        )
        path.write_text(text, encoding="utf-8")
        LOGGER.info("skill command=%s code=%s duration=%.3fs", argv[0] if argv else "?", code, duration)
