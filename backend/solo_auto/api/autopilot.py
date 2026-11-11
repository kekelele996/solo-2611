"""Unattended delivery routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from solo_auto.api.dependencies import context
from solo_auto.api.types import AutopilotRequest
from solo_auto.context import AppContext
from solo_auto.errors import ApiError

router = APIRouter(prefix="/api/autopilot")


@router.get("")
async def status(ctx: AppContext = Depends(context)) -> dict[str, Any]:
    return ctx.autopilot.snapshot()


@router.post("/start")
async def start(payload: AutopilotRequest, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    if not payload.task_ids:
        raise ApiError(422, "TASK_IDS_REQUIRED", "请至少选择一个任务")
    return await ctx.autopilot.enroll(payload.task_ids)


@router.post("/stop")
async def stop(payload: AutopilotRequest, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    return await ctx.autopilot.cancel(payload.task_ids or None)


@router.get("/{task_id}/log")
async def log(
    task_id: str,
    limit: int = Query(default=200, ge=1, le=1000),
    ctx: AppContext = Depends(context),
) -> dict[str, Any]:
    return ctx.autopilot.log_tail(task_id, limit)
