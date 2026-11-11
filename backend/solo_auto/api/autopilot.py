"""Unattended delivery routes."""

from __future__ import annotations

from typing import Any, cast

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse

from solo_auto.api.dependencies import context
from solo_auto.api.types import AutopilotRequest, AutopilotRetryRequest
from solo_auto.context import AppContext
from solo_auto.errors import ApiError

router = APIRouter(prefix="/api/autopilot")


async def _require_preflight(ctx: AppContext) -> dict[str, Any]:
    report = await ctx.preflight.run()
    if report["ok"]:
        return report
    failed = [
        str(item.get("message") or item.get("key") or "预检失败")
        for item in report.get("items", [])
        if not item.get("ok") and item.get("level") == "P0"
    ]
    ctx.intake.block("预检未通过，计划保持暂停：" + "；".join(failed[:3]))
    raise ApiError(409, "PREFLIGHT_FAILED", "自动交付预检未通过", detail=report)


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
    await _require_preflight(ctx)
    return await ctx.autopilot.start_tasks(payload.task_ids)


@router.post("/stop")
async def stop(payload: AutopilotRequest, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    if not payload.task_ids and ctx.intake.plan and ctx.intake.plan.get("status") in {"running", "paused"}:
        await ctx.intake.pause("自动交付已全部停止")
    return await ctx.autopilot.cancel(payload.task_ids or None, kill_containers=payload.kill_containers)


@router.post("/pause")
async def pause(ctx: AppContext = Depends(context)) -> dict[str, Any]:
    if ctx.intake.plan and ctx.intake.plan.get("status") == "running":
        await ctx.intake.pause("自动交付已暂停")
    result = ctx.autopilot.pause()
    if ctx.dispatcher.v3_enabled:
        await ctx.dispatcher.set_desired_state("paused")
    return result


@router.post("/resume")
async def resume(ctx: AppContext = Depends(context)) -> dict[str, Any]:
    await _require_preflight(ctx)
    if ctx.dispatcher.v3_enabled:
        await ctx.dispatcher.set_desired_state("running")
    plan = ctx.intake.plan
    if plan and plan.get("status") in {"running", "paused"}:
        intake_result = await ctx.intake.resume()
        raw_recovery = intake_result.get("recovery")
        recovery = cast(dict[str, Any], raw_recovery) if isinstance(raw_recovery, dict) else {}
        return {**ctx.autopilot.snapshot(), **recovery}
    return await ctx.autopilot.resume()


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
