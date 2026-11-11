"""Deterministic trace timeline and structural verification."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast


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
