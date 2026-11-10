"""Feedback gates, workbook export, statistics and file download."""

from __future__ import annotations

import asyncio
from datetime import date as date_type
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse

from solo_auto.api.dependencies import context
from solo_auto.api.types import ExportRequest, FeedbackAckRequest, FeedbackSaveRequest
from solo_auto.context import AppContext
from solo_auto.errors import ApiError

router = APIRouter(prefix="/api")


@router.post("/tasks/{task_id}/feedback/init")
async def init_feedback(task_id: str, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    path = await ctx.feedback.init(task_id)
    return {"ok": True, "path": path}


@router.get("/tasks/{task_id}/feedback")
async def get_feedback(task_id: str, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    return ctx.feedback.get(task_id)


@router.put("/tasks/{task_id}/feedback")
async def save_feedback(
    task_id: str,
    payload: FeedbackSaveRequest,
    ctx: AppContext = Depends(context),
) -> dict[str, Any]:
    return ctx.feedback.save(task_id, payload.text)


@router.post("/tasks/{task_id}/feedback/check")
async def check_feedback(task_id: str, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    return await ctx.feedback.check(task_id)


@router.post("/tasks/{task_id}/feedback/ack")
async def ack_feedback(
    task_id: str,
    payload: FeedbackAckRequest,
    ctx: AppContext = Depends(context),
) -> dict[str, Any]:
    return ctx.feedback.ack(task_id, acked=payload.acked)


@router.post("/export")
async def export(payload: ExportRequest, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    return await ctx.exports.export(task_ids=payload.task_ids, date=payload.date)


@router.get("/exports")
async def exports(ctx: AppContext = Depends(context)) -> dict[str, Any]:
    items = ctx.exports.history()
    return {"items": items, "total": len(items)}


@router.get("/exports/{name}/preview")
async def preview(name: str, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    return ctx.exports.preview(name)


@router.get("/exports/{name}/download")
async def download(name: str, ctx: AppContext = Depends(context)) -> FileResponse:
    path = ctx.exports.path(name)
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=path.name,
    )


@router.post("/exports/{name}/reveal")
async def reveal_export(name: str, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    path = ctx.exports.path(name)
    proc = await asyncio.create_subprocess_exec(
        "open",
        "-R",
        str(path),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise ApiError(
            500,
            "REVEAL_EXPORT_FAILED",
            stderr.decode("utf-8", errors="replace") or "Finder 定位失败",
        )
    return {"ok": True, "path": str(path)}


@router.get("/stats")
async def stats(
    from_date: str = Query(default="", alias="from"),
    to_date: str = Query(default="", alias="to"),
    ctx: AppContext = Depends(context),
) -> dict[str, Any]:
    start = from_date or date_type.today().isoformat()
    end = to_date or start
    return ctx.repo.stats(start, end)
