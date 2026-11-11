"""Usage-ledger aggregation and CSV export."""

from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, Depends, Query, Response

from solo_auto.api.dependencies import context
from solo_auto.context import AppContext

router = APIRouter(prefix="/api/usage")


@router.get("/projects")
async def projects(
    from_date: str = Query(default="", alias="from"),
    to_date: str = Query(default="", alias="to"),
    ctx: AppContext = Depends(context),
) -> dict[str, Any]:
    items = ctx.ledger.project_stats(start=from_date, end=to_date)
    return {
        "items": items,
        "total": len(items),
        "first_recorded_at": ctx.ledger.first_recorded_at(),
    }


@router.get("/projects/{project_id}/engineering")
async def engineering(
    project_id: str,
    from_date: str = Query(default="", alias="from"),
    to_date: str = Query(default="", alias="to"),
    ctx: AppContext = Depends(context),
) -> dict[str, Any]:
    items = ctx.ledger.engineering_stats(project_id=project_id, start=from_date, end=to_date)
    return {"items": items, "total": len(items)}


@router.get("/engineering/{engineering_id}")
async def records(engineering_id: str, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    items = ctx.ledger.records(engineering_id)
    for item in items:
        item["task_exists"] = bool(item.get("task_id") and ctx.repo.get_task(str(item["task_id"])))
    return {"items": items, "total": len(items)}


@router.get("/export.csv")
async def export_csv(
    from_date: str = Query(default="", alias="from"),
    to_date: str = Query(default="", alias="to"),
    ctx: AppContext = Depends(context),
) -> Response:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["项目", "工程", "使用", "交付", "提交", "交付率", "领取未用", "最近使用", "最近结局"])
    for item in ctx.ledger.engineering_stats(start=from_date, end=to_date):
        writer.writerow(
            [
                item.get("project_name") or item.get("project_id"),
                item.get("engineering_name") or item.get("engineering_id"),
                item.get("used") or 0,
                item.get("delivered") or 0,
                item.get("submitted") or 0,
                f"{float(item.get('delivery_rate') or 0):.1%}",
                item.get("claimed_unused") or 0,
                item.get("last_used_at") or "",
                item.get("last_outcome") or "",
            ]
        )
    return Response(
        "\ufeff" + output.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="usage.csv"'},
    )
