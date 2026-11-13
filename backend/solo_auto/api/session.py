"""Managed session controls exposed to the workbench."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends

from solo_auto.api.dependencies import context
from solo_auto.api.types import SessionAbortRequest, SessionContinueRequest
from solo_auto.context import AppContext
from solo_auto.errors import ApiError

router = APIRouter(prefix="/api")


def _task(ctx: AppContext, task_id: str) -> dict[str, Any]:
    task = ctx.repo.get_task(task_id)
    if task is None:
        raise ApiError(404, "TASK_NOT_FOUND", f"任务不存在：{task_id}")
    return task


async def _status(ctx: AppContext, task_id: str) -> dict[str, Any]:
    task = _task(ctx, task_id)
    result = await ctx.gateway.run_status(Path(str(task["task_root"])), task_id=task_id)
    if result.code != 0 or result.data is None:
        raise ApiError(
            409,
            "SESSION_STATUS_FAILED",
            result.err.strip() or "技能无法读取会话状态",
            detail={"stdout": result.out, "stderr": result.err},
        )
    return result.data


@router.post("/tasks/{task_id}/run/resume")
async def resume(task_id: str, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    task = _task(ctx, task_id)
    result = await ctx.gateway.call(
        ["resume", "--task-root", str(task["task_root"])],
        timeout=60,
        task_id=task_id,
    )
    if result.code != 0:
        raise ApiError(
            409,
            "SESSION_RESUME_FAILED",
            result.err.strip() or "会话无法接回",
            detail={"stdout": result.out, "stderr": result.err},
        )
    await ctx.reconciler.run()
    ctx.repo.add_event(
        "session.resume_requested",
        "会话接回请求已完成",
        actor="user",
        task_id=task_id,
        details=result.data or {},
    )
    return {"ok": True, "resume": result.data or {}, "status": await _status(ctx, task_id)}


@router.post("/tasks/{task_id}/run/continue")
async def send_continue(
    task_id: str,
    payload: SessionContinueRequest,
    ctx: AppContext = Depends(context),
) -> dict[str, Any]:
    task = _task(ctx, task_id)
    result = await ctx.gateway.call(
        [
            "continue",
            "--task-root",
            str(task["task_root"]),
            "--note",
            payload.note or "工作台人工继续",
        ],
        timeout=20,
        task_id=task_id,
    )
    if result.code != 0:
        message = result.err.strip() or result.out.strip() or "继续发送失败"
        status = 409 if "用尽" in message else 400
        raise ApiError(status, "SESSION_CONTINUE_REJECTED", message)
    ctx.repo.add_event(
        "session.continue_requested",
        "人工发送一次继续",
        actor="user",
        task_id=task_id,
        details=result.data or {},
    )
    return {"ok": True, **(result.data or {}), "status": await _status(ctx, task_id)}


@router.post("/tasks/{task_id}/run/abort")
async def abort(
    task_id: str,
    payload: SessionAbortRequest,
    ctx: AppContext = Depends(context),
) -> dict[str, Any]:
    task = _task(ctx, task_id)
    reason = payload.reason.strip()
    if not reason:
        raise ApiError(422, "ABORT_REASON_REQUIRED", "放弃任务必须填写原因")
    result = await ctx.gateway.call(
        [
            "abort",
            "--task-root",
            str(task["task_root"]),
            "--reason",
            reason,
            "--by",
            "human",
        ],
        timeout=90,
        task_id=task_id,
    )
    if result.code != 0:
        raise ApiError(
            409,
            "SESSION_ABORT_FAILED",
            result.err.strip() or "技能终止会话失败",
            detail={"stdout": result.out, "stderr": result.err},
        )
    await ctx.reconciler.run()
    ctx.repo.set_job_status(task_id, "aborted", notes=reason)
    ctx.ledger.mark_outcome(task_id, "void")
    await ctx.intake.terminal_session(task_id, reason="manual_abort")
    ctx.repo.add_event(
        "task.aborted",
        reason,
        level="warn",
        actor="user",
        task_id=task_id,
        details=result.data or {},
    )
    return {"ok": True, **(result.data or {}), "job_status": "aborted"}


@router.get("/tasks/{task_id}/run/session")
async def session_detail(task_id: str, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    status = await _status(ctx, task_id)
    return {
        **status,
        "rounds": ctx.repo.session_rounds(task_id),
    }


@router.get("/dispatch/session-stats")
async def session_stats(ctx: AppContext = Depends(context)) -> dict[str, Any]:
    return ctx.repo.session_stats()


@router.post("/dispatch/queue/{task_id}/resume")
async def resume_queue_item(task_id: str, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    task = _task(ctx, task_id)
    result = await ctx.gateway.call(
        ["resume", "--task-root", str(task["task_root"])],
        timeout=60,
        task_id=task_id,
    )
    if result.code != 0:
        raise ApiError(409, "SESSION_RESUME_FAILED", result.err.strip() or "会话无法接回")
    return {"ok": True, "resume": result.data or {}, "status": await _status(ctx, task_id)}
