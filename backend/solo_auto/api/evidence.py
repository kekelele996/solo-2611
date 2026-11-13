"""Timeline, diff, verification and artifact routes."""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse, PlainTextResponse

from solo_auto.api.dependencies import context
from solo_auto.api.types import RevealRequest, VerifyRequest
from solo_auto.context import AppContext
from solo_auto.errors import ApiError
from solo_auto.evidence.diff import diff_summary, file_diff
from solo_auto.evidence.runtime import run_verification
from solo_auto.evidence.trace import execution_trace, step_body, timeline, verify
from solo_auto.store.state import load_state

router = APIRouter(prefix="/api")


@router.post("/tasks/{task_id}/digest")
async def digest(task_id: str, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    task = _task(ctx, task_id)
    result = await ctx.gateway.call(
        ["digest", "--task-root", task["task_root"]],
        timeout=300,
        task_id=task_id,
    )
    if result.code != 0:
        raise ApiError(
            500,
            "DIGEST_FAILED",
            result.err.strip() or "轨迹时间线生成失败",
            detail={"stdout": result.out, "stderr": result.err},
        )
    await ctx.reconciler.run()
    return {"ok": True, "path": result.out.strip().splitlines()[-1] if result.out.strip() else ""}


@router.get("/tasks/{task_id}/timeline")
async def get_timeline(task_id: str, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    task = _task(ctx, task_id)
    trace = _trace_path(task)
    data = timeline(trace)
    data.pop("_bodies", None)
    return data


@router.get("/tasks/{task_id}/core-trace")
async def get_core_trace(
    task_id: str,
    limit: int = Query(default=500, ge=50, le=2000),
    ctx: AppContext = Depends(context),
) -> dict[str, Any]:
    task = _task(ctx, task_id)
    trace, live = _core_execution_trace_path(Path(task["task_root"]))
    data = execution_trace(trace, limit=limit)
    source = data["source"]
    if isinstance(source, dict):
        source["live"] = live
        source["mode"] = "live" if live else "final"
    return data


@router.get("/tasks/{task_id}/core-trace/raw")
async def download_core_trace(task_id: str, ctx: AppContext = Depends(context)) -> FileResponse:
    task = _task(ctx, task_id)
    trace, _ = _core_execution_trace_path(Path(task["task_root"]))
    return FileResponse(
        trace,
        media_type="application/x-ndjson",
        filename=f"{task_id}-core-trace.jsonl",
    )


@router.get("/tasks/{task_id}/timeline/steps/{number}")
async def get_timeline_step(
    task_id: str,
    number: int,
    ctx: AppContext = Depends(context),
) -> dict[str, Any]:
    task = _task(ctx, task_id)
    try:
        return step_body(_trace_path(task), number)
    except KeyError as exc:
        raise ApiError(404, "TIMELINE_STEP_NOT_FOUND", f"轨迹步骤不存在：{number}") from exc


@router.get("/tasks/{task_id}/diff")
async def get_diff(task_id: str, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    task = _task(ctx, task_id)
    envelope = load_state(Path(task["task_root"]))
    if not envelope.state:
        raise ApiError(409, "TASK_STATE_INCOMPATIBLE", envelope.error or "任务状态不可读")
    artifact = envelope.state.artifact.sha if envelope.state.artifact else ""
    return await diff_summary(Path(envelope.state.workspace), envelope.state.initialSha, artifact)


@router.get("/tasks/{task_id}/diff/file", response_class=PlainTextResponse)
async def get_file_diff(
    task_id: str,
    path: str = Query(min_length=1),
    ctx: AppContext = Depends(context),
) -> str:
    task = _task(ctx, task_id)
    envelope = load_state(Path(task["task_root"]))
    if not envelope.state:
        raise ApiError(409, "TASK_STATE_INCOMPATIBLE", envelope.error or "任务状态不可读")
    artifact = envelope.state.artifact.sha if envelope.state.artifact else ""
    try:
        return await file_diff(Path(envelope.state.workspace), envelope.state.initialSha, artifact, path)
    except ValueError as exc:
        raise ApiError(400, "DIFF_PATH_INVALID", str(exc)) from exc


@router.get("/tasks/{task_id}/verify")
async def get_verify(task_id: str, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    task = _task(ctx, task_id)
    envelope = load_state(Path(task["task_root"]))
    if not envelope.state:
        raise ApiError(409, "TASK_STATE_INCOMPATIBLE", envelope.error or "任务状态不可读")
    prompt_path = Path(envelope.state.promptPath) if envelope.state.promptPath else None
    structure = verify(_trace_path(task), prompt_path)
    runtime = _read_json(Path(task["task_root"]) / "monitor" / "runtime-verification.json")
    return {"structure": structure, "runtime": runtime}


@router.post("/tasks/{task_id}/verify")
async def post_verify(
    task_id: str,
    payload: VerifyRequest,
    ctx: AppContext = Depends(context),
) -> dict[str, Any]:
    task = _task(ctx, task_id)
    return await run_verification(
        task_id=task_id,
        task_root=Path(task["task_root"]),
        command=payload.cmd,
        repo=ctx.repo,
        bus=ctx.bus,
    )


@router.get("/tasks/{task_id}/artifacts")
async def artifacts(task_id: str, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    task = _task(ctx, task_id)
    root = Path(task["task_root"])
    envelope = load_state(root)
    state = envelope.state
    candidates = [
        ("trace", Path(state.session.trace)) if state and state.session else None,
        ("timeline", root / "delivery" / "轨迹时间线.md"),
        ("video", Path(state.recording.video)) if state and state.recording and state.recording.video else None,
        ("xlsx", root / "delivery" / "交付表.xlsx"),
        ("guide", root / "delivery" / "字段说明.md"),
        ("runtime_verify", root / "monitor" / "runtime-verification.json"),
        ("review_ack", root / "monitor" / "review-ack.json"),
        ("bundle", root / "delivery" / "产物材料.md"),
    ]
    items: list[dict[str, Any]] = []
    for item in candidates:
        if item is None:
            continue
        kind, path = item
        if not path.is_file():
            continue
        items.append(
            {
                "kind": kind,
                "path": str(path),
                "size": path.stat().st_size,
                "sha256": await _hash_file(path),
                "meta": {},
                "created_at": _created(path),
            }
        )
    return {"items": items, "total": len(items)}


@router.post("/tasks/{task_id}/reveal")
async def reveal(
    task_id: str,
    payload: RevealRequest,
    ctx: AppContext = Depends(context),
) -> dict[str, Any]:
    task = _task(ctx, task_id)
    root = Path(task["task_root"])
    if payload.kind == "vscode":
        code_binary = shutil.which("code") or shutil.which("cursor")
        if not code_binary:
            raise ApiError(503, "EDITOR_NOT_FOUND", "找不到 code 或 cursor 命令")
        proc = await asyncio.create_subprocess_exec(
            code_binary,
            str(root),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise ApiError(
                500,
                "EDITOR_OPEN_FAILED",
                stderr.decode("utf-8", errors="replace") or "编辑器打开失败",
            )
        return {"ok": True, "path": str(root)}
    mapping = {
        "task_root": root,
        "workspace": root / "workspace",
        "trace": root / "traces",
        "recording": root / "recording",
        "delivery": root / "delivery",
        "xlsx": root / "delivery" / "交付表.xlsx",
        "timeline": root / "delivery" / "轨迹时间线.md",
    }
    path = mapping.get(payload.kind)
    if path is None:
        raise ApiError(400, "REVEAL_KIND_INVALID", f"不支持定位的类型：{payload.kind}")
    if not path.exists():
        raise ApiError(404, "REVEAL_PATH_MISSING", f"文件或目录不存在：{path}")
    command = ["open", "-R", str(path)] if path.is_file() else ["open", str(path)]
    proc = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise ApiError(500, "REVEAL_FAILED", stderr.decode("utf-8", errors="replace") or "Finder 定位失败")
    return {"ok": True, "path": str(path)}


def _task(ctx: AppContext, task_id: str) -> dict[str, Any]:
    task = ctx.repo.get_task(task_id)
    if not task:
        raise ApiError(404, "TASK_NOT_FOUND", f"任务不存在：{task_id}")
    return task


def _trace_path(task: dict[str, Any]) -> Path:
    envelope = load_state(Path(task["task_root"]))
    if not envelope.state or not envelope.state.session or not envelope.state.session.trace:
        raise ApiError(409, "TRACE_NOT_AVAILABLE", "该任务还没有被接受的轨迹")
    return Path(envelope.state.session.trace)


def _core_execution_trace_path(task_root: Path) -> tuple[Path, bool]:
    envelope = load_state(task_root)
    state = envelope.state
    live_trace: Path | None = None
    live_status = ""
    if state and state.sessionRun:
        live_status = state.sessionRun.status
        if state.sessionRun.liveTraceDir:
            live_trace = _latest_jsonl(Path(state.sessionRun.liveTraceDir))
    active = live_status in {
        "launching",
        "prompt_sent",
        "prompt_delivery",
        "running",
        "continuing",
        "holding",
        "manual_input",
        "closing",
    }
    if active and live_trace is not None:
        return live_trace, True
    if state and state.session and state.session.trace:
        final_trace = Path(state.session.trace)
        if not final_trace.is_absolute():
            final_trace = task_root / final_trace
        if final_trace.is_file() and final_trace.stat().st_size:
            return final_trace, False
    for directory in (
        task_root / "traces",
        *sorted((task_root / "monitor" / "runs").glob("attempt-*/live")),
        *sorted((task_root / "monitor" / "runs").glob("attempt-*/traces")),
    ):
        candidate = _latest_jsonl(directory)
        if candidate is not None:
            return candidate, candidate == live_trace
    if live_trace is not None:
        return live_trace, True
    raise ApiError(409, "CORE_TRACE_NOT_AVAILABLE", "该任务还没有 Claude Code 核心执行轨迹")


def _latest_jsonl(directory: Path) -> Path | None:
    if not directory.is_dir():
        return None
    candidates = [
        path
        for path in directory.rglob("*.jsonl")
        if "subagents" not in path.parts and path.is_file() and path.stat().st_size > 0
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda path: (path.stat().st_mtime, path.stat().st_size))


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


async def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _created(path: Path) -> str:
    from datetime import datetime

    return datetime.fromtimestamp(path.stat().st_mtime).astimezone().replace(microsecond=0).isoformat()
