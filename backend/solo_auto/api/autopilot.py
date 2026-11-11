"""Unattended delivery routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse

from solo_auto.api.dependencies import context
from solo_auto.api.types import AutopilotRequest, AutopilotRetryRequest
from solo_auto.context import AppContext
from solo_auto.errors import ApiError

router = APIRouter(prefix="/api/autopilot")


@router.get("")
async def status(ctx: AppContext = Depends(context)) -> dict[str, Any]:
    return ctx.autopilot.snapshot()


@router.post("/preflight")
async def preflight(ctx: AppContext = Depends(context)) -> dict[str, Any]:
    return await ctx.preflight.run()


@router.post("/start")
async def start(payload: AutopilotRequest, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    if not payload.task_ids:
        raise ApiError(422, "TASK_IDS_REQUIRED", "请至少选择一个任务")
    for task_id in dict.fromkeys(payload.task_ids):
        if not ctx.repo.get_task(task_id):
            raise ApiError(404, "TASK_NOT_FOUND", f"任务不存在：{task_id}")
    report = await ctx.preflight.run()
    if not report["ok"]:
        raise ApiError(409, "PREFLIGHT_FAILED", "自动交付预检未通过", detail=report)
    return await ctx.autopilot.enroll(payload.task_ids)


@router.post("/stop")
async def stop(payload: AutopilotRequest, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    return await ctx.autopilot.cancel(payload.task_ids or None, kill_containers=payload.kill_containers)


@router.post("/pause")
async def pause(ctx: AppContext = Depends(context)) -> dict[str, Any]:
    return ctx.autopilot.pause()


@router.post("/resume")
async def resume(ctx: AppContext = Depends(context)) -> dict[str, Any]:
    return ctx.autopilot.resume()


@router.post("/{task_id}/retry")
async def retry(
    task_id: str,
    payload: AutopilotRetryRequest,
    ctx: AppContext = Depends(context),
) -> dict[str, Any]:
    return await ctx.autopilot.retry(task_id, from_stage=payload.from_stage)


@router.get("/{task_id}/stages")
async def stages(task_id: str, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    return {"items": ctx.autopilot.stages(task_id)}


@router.post("/{task_id}/chatgpt/open")
async def open_chatgpt(task_id: str, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    return await ctx.autopilot.open_chatgpt(task_id)


@router.get("/{task_id}/log")
async def log(
    task_id: str,
    limit: int = Query(default=200, ge=1, le=1000),
    stage: str = "",
    download: bool = False,
    ctx: AppContext = Depends(context),
) -> Any:
    result = ctx.autopilot.log_tail(task_id, limit, stage=stage)
    if download:
        path = result.get("file")
        if not path:
            raise ApiError(404, "AUTOPILOT_LOG_NOT_FOUND", "该阶段没有日志文件")
        return FileResponse(path, media_type="application/x-ndjson", filename=f"{task_id}-{result['stage']}.jsonl")
    return result


@router.get("/prompts")
async def prompts(ctx: AppContext = Depends(context)) -> dict[str, Any]:
    items = ctx.prompt_templates.listing()
    return {"items": items, "readonly": True}
