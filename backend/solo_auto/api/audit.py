"""Read-only audit queue and quality summaries."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from solo_auto.api.dependencies import context
from solo_auto.api.types import AuditDecisionRequest
from solo_auto.context import AppContext

router = APIRouter(prefix="/api")


@router.get("/audits")
async def audits(
    verdict: str | None = Query(default="pending"),
    ctx: AppContext = Depends(context),
) -> dict[str, Any]:
    rows = ctx.audits.list(verdict)
    for row in rows:
        task = ctx.repo.get_task(str(row["task_id"]))
        row["task_name"] = task["task_name"] if task else "任务已清理"
        row["task_type"] = task.get("task_type") if task else ""
        row["ack_by"] = task.get("ack_by") if task else ""
    return {"items": rows, "total": len(rows)}


@router.get("/audits/{task_id}")
async def audit_detail(task_id: str, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    return await ctx.audits.detail(task_id)


@router.post("/audits/{task_id}")
async def decide_audit(
    task_id: str,
    payload: AuditDecisionRequest,
    ctx: AppContext = Depends(context),
) -> dict[str, Any]:
    return await ctx.audits.decide(
        task_id,
        verdict=payload.verdict,
        reason=payload.reason,
        disposition=payload.disposition or "",
    )


@router.get("/quality/summary")
async def quality(
    from_date: str = Query(default="", alias="from"),
    to_date: str = Query(default="", alias="to"),
    ctx: AppContext = Depends(context),
) -> dict[str, Any]:
    return ctx.audits.quality_summary(from_date, to_date)
