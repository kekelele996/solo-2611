"""Launch a task as a visible ChatGPT desktop conversation and read its rollout."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import urllib.parse
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast

from solo_auto.errors import ApiError

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
            "不要只总结指令，直接开始执行，直到前 10 项门禁通过或出现真实阻断。"
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
