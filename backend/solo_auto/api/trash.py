"""Recoverable trash routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from solo_auto.api.dependencies import context
from solo_auto.context import AppContext

router = APIRouter(prefix="/api/trash")


@router.get("")
async def listing(ctx: AppContext = Depends(context)) -> dict[str, Any]:
    items = ctx.trash.list()
    return {
        "items": items,
        "total": len(items),
        "total_bytes": sum(int(item.get("size") or 0) for item in items),
        "retention_days": ctx.settings.trash.retention_days,
    }


@router.post("/{name}/restore")
async def restore(name: str, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    result = ctx.trash.restore(name)
    await ctx.reconciler.run()
    ctx.bus.publish("task.updated", {"restored": result.get("task_id")})
    return result


@router.delete("/{name}")
async def delete(name: str, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    return ctx.trash.delete(name)


@router.delete("")
async def empty(ctx: AppContext = Depends(context)) -> dict[str, Any]:
    return ctx.trash.empty()
