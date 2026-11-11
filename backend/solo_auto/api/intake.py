"""Production-plan routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from solo_auto.api.dependencies import context
from solo_auto.api.types import IntakeCreateRequest, IntakeStopRequest
from solo_auto.context import AppContext
from solo_auto.errors import ApiError

router = APIRouter(prefix="/api/intake")


@router.get("")
async def status(ctx: AppContext = Depends(context)) -> dict[str, Any]:
    return ctx.intake.snapshot()


@router.post("")
async def create(payload: IntakeCreateRequest, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    if not payload.project_id.strip():
        raise ApiError(400, "PROJECT_REQUIRED", "创建生产计划前必须选择项目")
    if not ctx.settings.autopilot.enabled:
        raise ApiError(409, "AUTOPILOT_DISABLED", "自动交付总开关已关闭，不能创建生产计划")
    report = await ctx.preflight.run()
    if not report["ok"]:
        raise ApiError(409, "PREFLIGHT_FAILED", "生产计划预检未通过", detail=report)
    return await ctx.intake.create(
        project_id=payload.project_id,
        target=payload.target,
        filters=payload.filters,
        exclude_delivered=payload.exclude_delivered,
        max_uses=payload.max_uses,
    )


@router.post("/pause")
async def pause(ctx: AppContext = Depends(context)) -> dict[str, Any]:
    return await ctx.intake.pause()


@router.post("/resume")
async def resume(ctx: AppContext = Depends(context)) -> dict[str, Any]:
    report = await ctx.preflight.run()
    if not report["ok"]:
        raise ApiError(409, "PREFLIGHT_FAILED", "生产计划预检未通过", detail=report)
    return await ctx.intake.resume()


@router.post("/stop")
async def stop(payload: IntakeStopRequest, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    return await ctx.intake.stop(release_unstarted=payload.release_unstarted)
