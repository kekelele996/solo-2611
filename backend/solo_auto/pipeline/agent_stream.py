"""Incremental, redacted conversion of Codex JSONL agent logs."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from solo_auto.events.bus import EventBus
from solo_auto.logging_setup import redact


def parse_event(event: dict[str, Any]) -> list[dict[str, str]]:
    kind = str(event.get("type") or "")
    item = event.get("item")
    if isinstance(item, dict) and kind == "item.completed":
        item_type = str(item.get("type") or "")
        if item_type == "command_execution":
            output = str(item.get("aggregated_output") or "").strip()
            text = f"$ {item.get('command')}  → exit {item.get('exit_code')}"
            if output:
                text += "\n" + output[-600:]
            return [{"kind": "command", "text": redact(text)}]
        if item_type in {"agent_message", "reasoning"}:
            return [
                {
                    "kind": "message" if item_type == "agent_message" else "thinking",
                    "text": redact(item.get("text") or ""),
                }
            ]
        if item_type == "error":
            return [{"kind": "error", "text": redact(item.get("message") or "")}]
        return [{"kind": "other", "text": item_type}]
    if kind in {"raw", "error"}:
        return [{"kind": kind, "text": redact(event.get("text") or event.get("message") or "")}]
    usage = event.get("usage")
    if isinstance(usage, dict):
        return []
    return []


def read_log(path: Path, limit: int = 200) -> list[dict[str, str]]:
    lines: list[dict[str, str]] = []
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for raw in handle:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    event = {"type": "raw", "text": raw}
                if isinstance(event, dict):
                    lines.extend(parse_event(event))
    except OSError:
        pass
    return lines[-limit:]


def verdict(path: Path) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    for raw in reversed(lines):
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            event = {"type": "raw", "text": raw}
        item = event.get("item") if isinstance(event, dict) else None
        text = str(item.get("text") or "") if isinstance(item, dict) else str(event.get("text") or "")
        for line in reversed(text.splitlines()):
            if "AUTOPILOT:" in line:
                return line.split("AUTOPILOT:", 1)[1].strip()[:300]
    return ""


def usage(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return result
    for raw in lines:
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        for source in (event, event.get("item"), event.get("usage")):
            if isinstance(source, dict) and isinstance(source.get("usage"), dict):
                result.update(source["usage"])
            elif isinstance(source, dict) and any(
                key in source for key in ("input_tokens", "output_tokens", "total_tokens")
            ):
                result.update(source)
    return result


class AgentLogStreamer:
    def __init__(
        self,
        *,
        path: Path,
        task_id: str,
        stage: str,
        bus: EventBus,
        interval: float = 0.25,
    ) -> None:
        self.path = path
        self.task_id = task_id
        self.stage = stage
        self.bus = bus
        self.interval = interval
        self.offset = 0
        self._buffer = ""

    async def run(self, done: asyncio.Event) -> None:
        while not done.is_set():
            frames = self.poll()
            if frames:
                self.bus.publish(
                    "autopilot.log",
                    {"task_id": self.task_id, "stage": self.stage, "frames": frames},
                )
            try:
                await asyncio.wait_for(done.wait(), timeout=self.interval)
            except TimeoutError:
                continue
        frames = self.poll()
        if frames:
            self.bus.publish(
                "autopilot.log",
                {"task_id": self.task_id, "stage": self.stage, "frames": frames},
            )

    def poll(self) -> list[dict[str, str]]:
        try:
            with self.path.open("r", encoding="utf-8", errors="replace") as handle:
                handle.seek(self.offset)
                chunk = handle.read()
                self.offset = handle.tell()
        except OSError:
            return []
        self._buffer += chunk
        lines = self._buffer.splitlines(keepends=True)
        if lines and not lines[-1].endswith(("\n", "\r")):
            self._buffer = lines.pop()
        else:
            self._buffer = ""
        frames: list[dict[str, str]] = []
        for raw in lines:
            raw = raw.strip()
            if not raw:
                continue
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                event = {"type": "raw", "text": raw}
            if isinstance(event, dict):
                frames.extend(parse_event(event))
        return frames

