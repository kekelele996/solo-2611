"""Deterministic trace timeline and structural verification."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, cast

EXECUTION_KINDS = ("command", "edit", "read", "search", "agent", "network", "other")
FILE_TOOLS = {"Read", "Write", "Edit", "MultiEdit", "NotebookEdit"}
EDIT_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}


def timeline(trace_path: Path) -> dict[str, Any]:
    events = _load(trace_path)
    results: dict[str, dict[str, Any]] = {}
    for event in events:
        content = _content(event)
        if event.get("type") == "user" and isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    results[str(block.get("tool_use_id"))] = block
    steps: list[dict[str, Any]] = []
    bodies: dict[int, dict[str, Any]] = {}
    for event in events:
        if event.get("type") != "assistant" or event.get("isSidechain"):
            continue
        content = _content(event)
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            number = len(steps) + 1
            tool_use_id = str(block.get("id") or "")
            result = results.get(tool_use_id, {})
            result_text = _result_text(result)
            name = str(block.get("name") or "")
            tool_input = cast(dict[str, Any], block.get("input") if isinstance(block.get("input"), dict) else {})
            steps.append(
                {
                    "no": number,
                    "kind": "tool",
                    "tool": name,
                    "summary": _summary(name, tool_input, result_text),
                    "status": "failed" if result.get("is_error") else "ok",
                    "ts": str(event.get("timestamp") or ""),
                    "body_ref": f"step-{number}",
                }
            )
            bodies[number] = {
                "input": tool_input,
                "output": result_text,
                "is_error": bool(result.get("is_error")),
                "tool_use_id": tool_use_id,
            }
    return {
        "steps": steps,
        "counts": {"total": len(steps), "failed": sum(step["status"] == "failed" for step in steps)},
        "_bodies": bodies,
    }


def execution_trace(trace_path: Path, *, limit: int = 500) -> dict[str, Any]:
    """Return only Claude Code tool execution, without chat or reasoning frames."""
    events = _load(trace_path)
    results: dict[str, dict[str, Any]] = {}
    for event in events:
        content = _content(event)
        if event.get("type") != "user" or not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                results[str(block.get("tool_use_id") or "")] = {
                    **block,
                    "_timestamp": str(event.get("timestamp") or ""),
                }

    steps: list[dict[str, Any]] = []
    seen: set[str] = set()
    session_ids: set[str] = set()
    models: set[str] = set()
    modified_files: set[str] = set()
    read_files: set[str] = set()
    for event in events:
        session_id = str(event.get("sessionId") or "")
        if session_id:
            session_ids.add(session_id)
        if event.get("type") != "assistant" or event.get("isSidechain"):
            continue
        message = event.get("message")
        if isinstance(message, dict) and message.get("model"):
            models.add(str(message["model"]))
        content = _content(event)
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            tool_use_id = str(block.get("id") or "")
            dedupe_key = tool_use_id or f"{len(steps)}:{block.get('name')}"
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            result = results.get(tool_use_id, {})
            name = str(block.get("name") or "")
            tool_input = cast(
                dict[str, Any],
                block.get("input") if isinstance(block.get("input"), dict) else {},
            )
            result_text = _result_text(result)
            status = "running" if not result else ("failed" if result.get("is_error") else "ok")
            kind = _execution_kind(name)
            target = _execution_target(name, tool_input)
            if name in EDIT_TOOLS and target:
                modified_files.add(target)
            if name == "Read" and target:
                read_files.add(target)
            started_at = str(event.get("timestamp") or "")
            result_at = str(result.get("_timestamp") or "")
            steps.append(
                {
                    "no": len(steps) + 1,
                    "kind": kind,
                    "tool": name,
                    "summary": _execution_summary(name, tool_input, result_text),
                    "note": _execution_note(name, tool_input),
                    "target": target,
                    "status": status,
                    "ts": started_at,
                    "duration_ms": _elapsed_ms(started_at, result_at),
                    "input": _bounded_value(tool_input),
                    "output": _short_text(result_text, 4000),
                    "output_chars": len(result_text),
                    "tool_use_id": tool_use_id,
                }
            )

    by_kind = {kind: sum(step["kind"] == kind for step in steps) for kind in EXECUTION_KINDS}
    updated_at = ""
    if trace_path.is_file():
        updated_at = (
            datetime.fromtimestamp(trace_path.stat().st_mtime)
            .astimezone()
            .replace(microsecond=0)
            .isoformat()
        )
    return {
        "steps": steps[-max(1, limit) :],
        "counts": {
            "total": len(steps),
            "failed": sum(step["status"] == "failed" for step in steps),
            "running": sum(step["status"] == "running" for step in steps),
            "commands": by_kind["command"],
            "edits": by_kind["edit"],
            "reads": by_kind["read"],
            "searches": by_kind["search"],
            "by_kind": by_kind,
        },
        "modified_files": sorted(modified_files),
        "read_files": sorted(read_files),
        "source": {
            "path": str(trace_path),
            "session_id": next(iter(session_ids)) if len(session_ids) == 1 else "",
            "models": sorted(models),
            "updated_at": updated_at,
        },
    }


def verify(trace_path: Path, prompt_path: Path | None = None) -> dict[str, Any]:
    events = _load(trace_path)
    session_ids = sorted({str(event["sessionId"]) for event in events if event.get("sessionId")})
    users = [event for event in events if _is_real_user(event)]
    prompt_id = str(users[0].get("promptId") or "") if len(users) == 1 else ""
    assistants = [event for event in events if event.get("type") == "assistant" and not event.get("isSidechain")]
    final = assistants[-1] if assistants else {}
    message = cast(dict[str, Any], final.get("message") if isinstance(final.get("message"), dict) else {})
    stop_reason = message.get("stop_reason")
    prompt_matches: bool | None = None
    if prompt_path and prompt_path.is_file() and len(users) == 1:
        expected = _norm(prompt_path.read_text(encoding="utf-8"))
        actual = _norm(_text(_content(users[0])))
        prompt_matches = expected == actual
    errors: list[str] = []
    if len(session_ids) != 1:
        errors.append(f"sessionId 不唯一：{session_ids}")
    if len(users) != 1:
        errors.append(f"真人 user 消息数量为 {len(users)}，应为 1")
    if not prompt_id:
        errors.append("缺少 PromptID")
    if stop_reason != "end_turn":
        errors.append(f"最终 stop_reason 为 {stop_reason!r}，不是 end_turn")
    if prompt_matches is False:
        errors.append("轨迹题面与已安装题面不一致")
    return {
        "ok": not errors,
        "errors": errors,
        "session_id": session_ids[0] if len(session_ids) == 1 else "",
        "session_count": len(session_ids),
        "user_messages": len(users),
        "prompt_id": prompt_id,
        "stop_reason": stop_reason,
        "prompt_matches": prompt_matches,
        "assistant_messages": len(assistants),
    }


def step_body(trace_path: Path, number: int) -> dict[str, Any]:
    data = timeline(trace_path)
    body = data["_bodies"].get(number)
    if body is None:
        raise KeyError(number)
    return cast(dict[str, Any], body)


def _load(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    if not path.is_file():
        return events
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            events.append(item)
    return events


def _content(event: dict[str, Any]) -> Any:
    message = event.get("message")
    return message.get("content") if isinstance(message, dict) else None


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(block.get("text") or "") for block in content if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


def _result_text(block: dict[str, Any]) -> str:
    content = block.get("content")
    if isinstance(content, list):
        return "\n".join(str(item.get("text") or item) for item in content if isinstance(item, dict))
    return str(content or "")


def _summary(name: str, data: dict[str, Any], result: str) -> str:
    if name == "Bash":
        return _short(str(data.get("command") or ""), 160)
    if name in {"Read", "Write", "Edit", "MultiEdit"}:
        return _short(str(data.get("file_path") or data.get("notebook_path") or ""), 160)
    if name in {"Glob", "Grep"}:
        return _short(str(data.get("pattern") or ""), 120)
    return _short(result or json.dumps(data, ensure_ascii=False), 140)


def _execution_kind(name: str) -> str:
    if name in {"Bash", "BashOutput", "KillShell"}:
        return "command"
    if name in EDIT_TOOLS:
        return "edit"
    if name == "Read":
        return "read"
    if name in {"Glob", "Grep", "LS"}:
        return "search"
    if name in {"Task", "Agent"}:
        return "agent"
    if name in {"WebFetch", "WebSearch"}:
        return "network"
    return "other"


def _execution_target(name: str, data: dict[str, Any]) -> str:
    if name in FILE_TOOLS:
        return str(data.get("file_path") or data.get("notebook_path") or "")
    if name in {"Glob", "Grep", "LS"}:
        return str(data.get("path") or "")
    if name in {"WebFetch", "WebSearch"}:
        return str(data.get("url") or data.get("query") or "")
    return ""


def _execution_summary(name: str, data: dict[str, Any], result: str) -> str:
    if name == "Bash":
        return _short(str(data.get("command") or ""), 220)
    if name in {"BashOutput", "KillShell"}:
        return _short(str(data.get("shell_id") or data.get("shellId") or name), 120)
    if name in FILE_TOOLS:
        return _short(_execution_target(name, data), 220)
    if name in {"Glob", "Grep", "LS"}:
        query = str(data.get("pattern") or data.get("path") or "")
        return _short(query, 180)
    if name in {"Task", "Agent"}:
        return _short(str(data.get("description") or data.get("prompt") or name), 220)
    if name in {"WebFetch", "WebSearch"}:
        return _short(_execution_target(name, data), 180)
    return _short(result or json.dumps(data, ensure_ascii=False), 180)


def _execution_note(name: str, data: dict[str, Any]) -> str:
    if name == "Bash":
        return _short(str(data.get("description") or ""), 120)
    if name == "Read":
        offset = data.get("offset")
        line_limit = data.get("limit")
        if offset or line_limit:
            return f"从第 {offset or 1} 行读取 {line_limit or '剩余'} 行"
    if name == "Write":
        text = str(data.get("content") or "")
        return f"写入 {len(text.splitlines())} 行"
    if name in {"Edit", "MultiEdit"}:
        edits = data.get("edits")
        return f"修改 {len(edits)} 处" if isinstance(edits, list) else "修改文件内容"
    if name == "Grep":
        return f"路径 {data.get('path') or '.'}"
    if name == "Task":
        return str(data.get("subagent_type") or "")
    return ""


def _bounded_value(value: Any, limit: int = 3000) -> Any:
    if isinstance(value, str):
        return _short_text(value, limit)
    if isinstance(value, dict):
        return {str(key): _bounded_value(item, limit) for key, item in value.items()}
    if isinstance(value, list):
        return [_bounded_value(item, limit) for item in value]
    return value


def _short_text(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def _elapsed_ms(started_at: str, ended_at: str) -> int | None:
    if not started_at or not ended_at:
        return None
    try:
        start = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        end = datetime.fromisoformat(ended_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0, round((end - start).total_seconds() * 1000))


def _short(value: str, limit: int) -> str:
    normalized = " ".join(value.split())
    return normalized if len(normalized) <= limit else normalized[: limit - 1] + "…"


def _is_real_user(event: dict[str, Any]) -> bool:
    if event.get("type") != "user" or event.get("isMeta") or event.get("isSidechain"):
        return False
    content = _content(event)
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        if any(isinstance(block, dict) and block.get("type") == "tool_result" for block in content):
            return False
        return any(isinstance(block, dict) and block.get("type") in {"text", "image"} for block in content)
    return False


def _norm(text: str) -> str:
    return "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").strip().splitlines())
