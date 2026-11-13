"""Launch a task as a visible ChatGPT desktop conversation and read its rollout."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast

from solo_auto.errors import ApiError

MARKER_RE = re.compile(r"solo-auto-chatgpt:([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}):([0-9a-z]+)")
TURN_EVENTS = frozenset({"task_started", "task_complete", "turn_aborted", "stream_error", "error"})
TERMINAL_TURN_EVENTS = frozenset({"task_complete", "turn_aborted", "stream_error", "error"})
TOOL_ITEMS = frozenset(
    {
        "function_call",
        "function_call_output",
        "custom_tool_call",
        "custom_tool_call_output",
        "local_shell_call",
        "local_shell_call_output",
    }
)
# 标识只认会话开头几条消息里的用户指令，避免把“提到了标识”的其他会话误算进来。
MARKER_SCAN_LINES = 80

DEFAULT_HELPERS = (
    Path.home() / "no_cloud" / "common" / "solo2-monitor" / "CodexQueuePush.app" / "Contents" / "MacOS" / "CodexQueuePush",
    Path.home()
    / "no_cloud"
    / "common"
    / "solo2-migration-20260915"
    / "payload"
    / "program"
    / "solo2-monitor"
    / "CodexQueuePush.app"
    / "Contents"
    / "MacOS"
    / "CodexQueuePush",
)
SUBMIT_PENDING_FLAG = "--submit-only"


class ChatGPTDesktop:
    def __init__(self, *, home: Path, helper: str = "", sessions_dir: Path | None = None) -> None:
        self.home = home
        self.app_path = Path("/Applications/ChatGPT.app")
        self.helper = Path(helper).expanduser() if helper else self._find_helper()
        self.sessions_dir = sessions_dir or Path(
            os.environ.get("CODEX_SESSIONS_DIR") or Path.home() / ".codex" / "sessions"
        )

    def available(self) -> bool:
        return self.app_path.is_dir() and (self.helper is not None or shutil.which("open") is not None)

    def launch(
        self,
        *,
        task_id: str,
        task_name: str,
        task_root: Path,
        prompt: str,
        folder_id: str = "",
    ) -> dict[str, Any]:
        if not self.app_path.is_dir():
            raise ApiError(409, "CHATGPT_APP_MISSING", "未找到 ChatGPT.app，无法创建桌面会话")
        marker = f"solo-auto-chatgpt:{task_id}:{uuid.uuid4().hex[:12]}"
        marked_prompt = (
            f"{prompt.strip()}\n\n"
            f"本次会话标识：`{marker}`\n"
            f"唯一任务名：`{task_name}`\n"
            f"任务目录：`{task_root}`\n"
        )
        directory = self.home / "desktop" / task_id
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        grouped_workdir = self.home / "chatgpt-workspaces" / datetime.now().strftime("%Y-%m-%d")
        grouped_workdir.mkdir(parents=True, exist_ok=True)
        prompt_path = directory / f"chatgpt-{stamp}.md"
        prompt_path.write_text(marked_prompt, encoding="utf-8")
        link_prompt = (
            "使用 `$solo-2611`，读取并严格执行下面的本地任务指令文件：\n"
            f"`{prompt_path}`\n"
            f"唯一任务标识：`{marker}`\n"
            f"唯一任务名：`{task_name}`\n"
            f"任务目录：`{task_root}`\n"
            "不要只总结指令，直接开始执行，直到前 10 项门禁通过或出现真实阻断。\n"
            "真实阻断必须使用 `AUTOPILOT: BLOCKED <原因>` 结束，平台会自动停止重试；"
            "只有可恢复的失败才使用 `AUTOPILOT: FAILED <原因>`。\n"
            "完成后提交到 SOLO2 平台，用户名：使用本地工作台配置的用户。\n"
            "提交流程只写在本会话指令中，绝对不要写入正式题面文件。"
        )
        link = build_deep_link(workdir=grouped_workdir, prompt=link_prompt, folder_id=folder_id)
        (directory / f"chatgpt-{stamp}.url.txt").write_text(link + "\n", encoding="utf-8")
        opened_with = "open"
        if self.helper and self.helper.is_file() and os.access(self.helper, os.X_OK):
            result = subprocess.run(
                [str(self.helper), link],
                capture_output=True,
                text=True,
                timeout=45,
                check=False,
            )
            opened_with = str(self.helper)
            if result.returncode != 0:
                message = (result.stderr or result.stdout).strip() or f"CodexQueuePush 退出码 {result.returncode}"
                if result.returncode == 3:
                    message = "CodexQueuePush 缺少辅助功能权限，请在系统设置中授权"
                raise ApiError(500, "CHATGPT_LAUNCH_FAILED", message)
        else:
            result = subprocess.run(
                ["open", link],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if result.returncode != 0:
                raise ApiError(
                    500,
                    "CHATGPT_LAUNCH_FAILED",
                    (result.stderr or result.stdout).strip() or "打开 ChatGPT 深链接失败",
                )
        return {
            "marker": marker,
            "prompt_path": str(prompt_path),
            "deep_link": link,
            "opened_with": opened_with,
            "workdir": str(grouped_workdir),
            "started_at": datetime.now().astimezone().replace(microsecond=0).isoformat(),
        }

    def submit_pending_prompt(self, link: str = "") -> dict[str, Any]:
        """Ask the trusted helper to submit the prompt already present in ChatGPT.

        This deliberately does not reopen the deep link. Reopening can create a
        second thread when the first one was merely slow to accept its prompt.
        """
        if not self.helper or not self.helper.is_file() or not os.access(self.helper, os.X_OK):
            return {
                "ok": False,
                "opened_with": "",
                "message": "未找到支持补发的 CodexQueuePush",
            }
        args = [str(self.helper), SUBMIT_PENDING_FLAG]
        if link:
            args.append(link)
        try:
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return {
                "ok": False,
                "opened_with": str(self.helper),
                "message": "补按发送超时",
            }
        except OSError as exc:
            return {
                "ok": False,
                "opened_with": str(self.helper),
                "message": f"补按发送失败：{exc}",
            }
        if result.returncode == 0:
            return {
                "ok": True,
                "opened_with": str(self.helper),
                "message": "已补按发送",
            }
        message = (result.stderr or result.stdout).strip()
        if result.returncode == 3:
            message = "CodexQueuePush 缺少辅助功能权限，请在系统设置中授权"
        elif result.returncode == 2 and not message:
            message = "当前 CodexQueuePush 版本不支持补按发送"
        return {
            "ok": False,
            "opened_with": str(self.helper),
            "message": message or f"CodexQueuePush 补发退出码 {result.returncode}",
        }

    def find_rollout(self, marker: str, *, started_at: datetime, workdir: Path) -> Path | None:
        if not self.sessions_dir.is_dir():
            return None
        cutoff = (started_at - timedelta(minutes=2)).timestamp()
        today = datetime.now().date()
        day = started_at.date()
        days: list[Path] = []
        while day <= today and len(days) < 4:
            days.append(self.sessions_dir / f"{day:%Y}" / f"{day:%m}" / f"{day:%d}")
            day += timedelta(days=1)
        candidates: list[Path] = []
        for directory in days:
            if not directory.is_dir():
                continue
            for path in directory.glob("rollout-*.jsonl"):
                try:
                    if path.stat().st_mtime >= cutoff:
                        candidates.append(path)
                except OSError:
                    continue
        for path in sorted(candidates, key=lambda item: item.stat().st_mtime, reverse=True):
            found_marker = False
            rollout_cwd = ""
            try:
                with path.open(encoding="utf-8", errors="replace") as handle:
                    for line in handle:
                        if marker in line:
                            found_marker = True
                        if not rollout_cwd and '"session_meta"' in line:
                            try:
                                event = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            payload = event.get("payload") if isinstance(event, dict) else None
                            if isinstance(payload, dict):
                                rollout_cwd = str(payload.get("cwd") or "")
                        if found_marker and rollout_cwd:
                            break
            except OSError:
                continue
            if found_marker and rollout_cwd:
                try:
                    if Path(rollout_cwd).resolve() == workdir.resolve():
                        return path
                except OSError:
                    continue
        return None

    def _find_helper(self) -> Path | None:
        configured = os.environ.get("SOLO_AUTO_CODEX_PUSH_HELPER", "").strip()
        candidates = [Path(configured)] if configured else []
        candidates.extend(DEFAULT_HELPERS)
        discovered = shutil.which("CodexQueuePush")
        if discovered:
            candidates.append(Path(discovered))
        for candidate in candidates:
            try:
                if candidate.is_file() and os.access(candidate, os.X_OK):
                    return candidate.resolve()
            except OSError:
                continue
        return None


@dataclass(slots=True)
class RolloutInfo:
    """Incrementally parsed facts about one ChatGPT desktop rollout file."""

    path: Path
    offset: int = 0
    lines: int = 0
    marker: str = ""
    task_id: str = ""
    cwd: str = ""
    turn_state: str = ""
    error_text: str = ""
    last_tool_at: float = 0.0
    mtime: float = 0.0

    @property
    def terminal(self) -> bool:
        return self.turn_state in TERMINAL_TURN_EVENTS


class RolloutIndex:
    """Tracks every rollout the platform launched, whether or not it still follows it.

    ChatGPT desktop threads cannot be stopped from outside. A thread the
    platform gave up on keeps running and consuming quota, so capacity must be
    measured from the rollouts themselves rather than from bookkeeping. Each
    file is read once and then only from its last complete line onwards.
    """

    def __init__(self, sessions_dir: Path) -> None:
        self.sessions_dir = sessions_dir
        self.items: dict[str, RolloutInfo] = {}
        self._lock = threading.RLock()

    def refresh(self, *, window_seconds: float, now: float | None = None) -> None:
        current = time.time() if now is None else now
        cutoff = current - window_seconds
        seen: set[str] = set()
        for path in self._recent_files(cutoff, current):
            seen.add(str(path))
            self.update(path)
        # 仍被视为存活的会话也要重新确认一次，文件被删或改动时间回退都不能让它一直占名额。
        for info in self.snapshot():
            if info.marker and not info.terminal and str(info.path) not in seen:
                self.update(info.path)
        with self._lock:
            for key in [key for key, info in self.items.items() if info.mtime < cutoff - 86400]:
                self.items.pop(key, None)

    def update(self, path: Path) -> RolloutInfo:
        with self._lock:
            return self._update(path)

    def _update(self, path: Path) -> RolloutInfo:
        key = str(path)
        info = self.items.get(key)
        if info is None:
            info = RolloutInfo(path=path)
            self.items[key] = info
        try:
            stat = path.stat()
        except OSError:
            info.mtime = 0.0
            return info
        info.mtime = stat.st_mtime
        if stat.st_size < info.offset:
            info.offset = 0
            info.lines = 0
            info.turn_state = ""
            info.error_text = ""
            info.last_tool_at = 0.0
        if stat.st_size == info.offset:
            return info
        try:
            with path.open("rb") as handle:
                handle.seek(info.offset)
                while True:
                    raw = handle.readline()
                    if not raw or not raw.endswith(b"\n"):
                        break
                    info.offset = handle.tell()
                    info.lines += 1
                    self._consume(info, raw)
        except OSError:
            return info
        return info

    def find(self, marker: str) -> RolloutInfo | None:
        with self._lock:
            matches = [info for info in self.items.values() if info.marker == marker]
        return max(matches, key=lambda info: info.mtime) if matches else None

    def snapshot(self) -> list[RolloutInfo]:
        with self._lock:
            return list(self.items.values())

    def live(self, *, quiet_seconds: float, now: float | None = None) -> list[RolloutInfo]:
        """Platform-launched rollouts that still wrote recently and whose turn has not ended."""
        current = time.time() if now is None else now
        return [
            info
            for info in self.snapshot()
            if info.marker and not info.terminal and current - info.mtime <= quiet_seconds
        ]

    def _consume(self, info: RolloutInfo, raw: bytes) -> None:
        needs_marker = not info.marker and info.lines <= MARKER_SCAN_LINES
        if not needs_marker and b'"event_msg"' not in raw and not any(
            item.encode() in raw for item in ("_call", "_call_output")
        ):
            return
        try:
            event = json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            return
        if not isinstance(event, dict):
            return
        kind = str(event.get("type") or "")
        payload = event.get("payload")
        if not isinstance(payload, dict):
            return
        item_type = str(payload.get("type") or "")
        if kind == "session_meta" and not info.cwd:
            info.cwd = str(payload.get("cwd") or "")
            return
        if needs_marker and kind == "response_item" and item_type == "message" and payload.get("role") == "user":
            match = MARKER_RE.search(json.dumps(payload.get("content"), ensure_ascii=False))
            if match:
                info.marker = match.group(0)
                info.task_id = match.group(1)
            return
        if kind == "event_msg" and item_type in TURN_EVENTS:
            info.turn_state = item_type
            if item_type in {"stream_error", "error", "turn_aborted"}:
                message = payload.get("message") or payload.get("reason") or payload
                info.error_text = (message if isinstance(message, str) else json.dumps(message, ensure_ascii=False))[:2000]
            elif item_type == "task_started":
                info.error_text = ""
            return
        if kind == "response_item" and item_type in TOOL_ITEMS:
            stamp = _event_epoch(event)
            if stamp:
                info.last_tool_at = max(info.last_tool_at, stamp)

    def _recent_files(self, cutoff: float, now: float) -> list[Path]:
        if not self.sessions_dir.is_dir():
            return []
        files: list[Path] = []
        day = datetime.fromtimestamp(min(cutoff, now - 86400)).date()
        last = datetime.fromtimestamp(now).date()
        while day <= last:
            directory = self.sessions_dir / f"{day:%Y}" / f"{day:%m}" / f"{day:%d}"
            day += timedelta(days=1)
            if not directory.is_dir():
                continue
            for path in directory.glob("rollout-*.jsonl"):
                try:
                    if path.stat().st_mtime >= cutoff:
                        files.append(path)
                except OSError:
                    continue
        return files


def _event_epoch(event: dict[str, Any]) -> float:
    value = str(event.get("timestamp") or "")
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def build_deep_link(*, workdir: Path, prompt: str, folder_id: str = "") -> str:
    params = {
        "path": str(workdir),
        "mode": "work",
        "prompt": prompt,
    }
    if folder_id:
        params["projectId"] = folder_id
    return "codex://threads/new?" + urllib.parse.urlencode(params)


def rollout_delta(path: Path, offset: int) -> tuple[list[dict[str, str]], int]:
    try:
        size = path.stat().st_size
    except OSError:
        return [], offset
    if size < offset:
        offset = 0
    frames: list[dict[str, str]] = []
    try:
        with path.open("rb") as handle:
            handle.seek(offset)
            while True:
                raw = handle.readline()
                if not raw:
                    break
                offset = handle.tell()
                try:
                    event = json.loads(raw.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    frames.extend(parse_rollout_event(event))
    except OSError:
        return frames, offset
    return frames, offset


def read_rollout(path: Path, limit: int = 200) -> list[dict[str, str]]:
    frames, _ = rollout_delta(path, 0)
    return frames[-limit:]


def rollout_terminal(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    terminal = ""
    for raw in text.splitlines():
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") != "event_msg":
            continue
        payload = event.get("payload")
        kind = str(payload.get("type") or "") if isinstance(payload, dict) else ""
        if kind in {"task_complete", "turn_aborted", "stream_error", "error"}:
            terminal = kind
    return terminal


def rollout_last_assistant(path: Path) -> str:
    text = ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    for raw in reversed(lines):
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") != "response_item":
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict) or payload.get("type") != "message" or payload.get("role") != "assistant":
            continue
        content = payload.get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = "\n".join(
                str(item.get("text") or item.get("output_text") or "")
                for item in content
                if isinstance(item, dict)
            ).strip()
        if text:
            break
    return text[-4000:]


def thread_id_from_rollout(path: Path) -> str:
    match = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$", path.name)
    return match.group(1) if match else ""


def parse_rollout_event(event: dict[str, Any]) -> list[dict[str, str]]:
    kind = str(event.get("type") or "")
    payload = cast(dict[str, Any], event.get("payload") if isinstance(event.get("payload"), dict) else {})
    if kind == "event_msg":
        message_type = str(payload.get("type") or "")
        if message_type in {"turn_aborted", "stream_error", "error"}:
            return [{"kind": "error", "text": json.dumps(payload, ensure_ascii=False)[:2000]}]
        if message_type == "task_started":
            return [{"kind": "message", "text": "ChatGPT 已开始处理"}]
        return []
    if kind != "response_item":
        return []
    item_type = str(payload.get("type") or "")
    if item_type == "message" and payload.get("role") == "assistant":
        content = payload.get("content")
        text = ""
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = "\n".join(
                str(item.get("text") or item.get("output_text") or "")
                for item in content
                if isinstance(item, dict)
            ).strip()
        return [{"kind": "message", "text": text}] if text else []
    if item_type == "reasoning":
        text = str(payload.get("summary_text") or payload.get("summary") or "")
        return [{"kind": "thinking", "text": text}] if text else []
    if item_type == "function_call":
        arguments = payload.get("arguments")
        command = ""
        if isinstance(arguments, str):
            try:
                parsed = json.loads(arguments)
            except json.JSONDecodeError:
                parsed = {}
            if isinstance(parsed, dict):
                command = str(parsed.get("cmd") or parsed.get("command") or "")
        if command:
            return [{"kind": "command", "text": command}]
        return [{"kind": "other", "text": f"{payload.get('name') or 'tool'} {arguments or ''}"}]
    if item_type == "function_call_output" and payload.get("output"):
        return [{"kind": "command", "text": str(payload["output"])[:2000]}]
    return []


def latest_activity(paths: list[Path]) -> datetime | None:
    latest = 0.0
    for path in paths:
        if not path:
            continue
        try:
            latest = max(latest, path.stat().st_mtime)
        except OSError:
            continue
    if not latest:
        return None
    return datetime.fromtimestamp(latest).astimezone()
