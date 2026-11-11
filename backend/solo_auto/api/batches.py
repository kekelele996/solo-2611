"""Batch health checks and submission history."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse

from solo_auto.api.dependencies import context
from solo_auto.api.types import BatchCheckRequest, BatchCreateRequest
from solo_auto.context import AppContext
from solo_auto.errors import ApiError

router = APIRouter(prefix="/api/batches")


@router.post("/check")
async def check(payload: BatchCheckRequest, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    return ctx.batch.check(payload.task_ids, include_exported=payload.include_exported)


@router.post("")
async def create(payload: BatchCreateRequest, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    return await ctx.batch.create(
        payload.task_ids,
        include_exported=payload.include_exported,
        acknowledge_warnings=payload.acknowledge_warnings,
    )


@router.get("")
async def listing(ctx: AppContext = Depends(context)) -> dict[str, Any]:
    items = ctx.repo.batches()
    for item in items:
        path = Path(str(item.get("xlsx_path") or ""))
        item["exists"] = path.is_file()
        item["size"] = path.stat().st_size if path.is_file() else 0
    return {"items": items, "total": len(items)}


@router.get("/{batch_id}")
async def detail(batch_id: str, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    batch = ctx.repo.batch(batch_id)
    if not batch:
        raise ApiError(404, "BATCH_NOT_FOUND", f"批次不存在：{batch_id}")
    manifest = Path(str(batch["xlsx_path"])).parent / "manifest.json"
    batch["manifest"] = _read_text(manifest)
    return batch


@router.post("/{batch_id}/bundle")
async def bundle(batch_id: str, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    return ctx.batch.bundle(batch_id)


@router.get("/{batch_id}/download")
async def download(batch_id: str, ctx: AppContext = Depends(context)) -> FileResponse:
    batch = ctx.repo.batch(batch_id)
    if not batch:
        raise ApiError(404, "BATCH_NOT_FOUND", f"批次不存在：{batch_id}")
    path = Path(str(batch["xlsx_path"]))
    if not path.is_file():
        raise ApiError(404, "BATCH_FILE_NOT_FOUND", "批次文件不存在")
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=path.name,
    )


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""
