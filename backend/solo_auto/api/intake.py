"""Production-plan routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from solo_auto.api.dependencies import context
from solo_auto.api.profiles import serialize_account_changes
from solo_auto.api.types import IntakeCreateRequest, IntakeRetargetRequest, IntakeStopRequest
from solo_auto.context import AppContext
from solo_auto.errors import ApiError

router = APIRouter(prefix="/api/intake")


@router.get("")
async def status(ctx: AppContext = Depends(context)) -> dict[str, Any]:
    return ctx.intake.snapshot()


@router.post("", dependencies=[Depends(serialize_account_changes)])
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
        mode=payload.mode,
        daily_cap=payload.daily_cap,
    )


@router.post("/project", dependencies=[Depends(serialize_account_changes)])
async def retarget_project(payload: IntakeRetargetRequest, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    return await ctx.intake.retarget_project(payload.project_id)


@router.post("/pause", dependencies=[Depends(serialize_account_changes)])
async def pause(ctx: AppContext = Depends(context)) -> dict[str, Any]:
    return await ctx.intake.pause()


@router.post("/resume", dependencies=[Depends(serialize_account_changes)])
async def resume(ctx: AppContext = Depends(context)) -> dict[str, Any]:
    report = await ctx.preflight.run()
    if not report["ok"]:
        failed = [
            str(item.get("message") or item.get("key") or "预检失败")
            for item in report.get("items", [])
            if not item.get("ok") and item.get("level") == "P0"
        ]
        ctx.intake.block("预检未通过，计划保持暂停：" + "；".join(failed[:3]))
        raise ApiError(409, "PREFLIGHT_FAILED", "生产计划预检未通过", detail=report)
    return await ctx.intake.resume()


@router.post("/stop", dependencies=[Depends(serialize_account_changes)])
async def stop(payload: IntakeStopRequest, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    return await ctx.intake.stop(release_unstarted=payload.release_unstarted)
