"""Project-scoped prompt library import and export."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response

from solo_auto.api.dependencies import context
from solo_auto.api.types import PromptDedupeRequest, PromptLibraryImportRequest
from solo_auto.context import AppContext
from solo_auto.errors import ApiError
from solo_auto.pipeline.prompt_history import PromptHistory

router = APIRouter(prefix="/api/source/prompts")


def _history(ctx: AppContext) -> PromptHistory:
    return PromptHistory(ctx.repo, ctx.paths.home / "prompt-history.jsonl")


@router.get("")
async def list_prompts(
    project_id: str = Query(alias="project_id"),
    ctx: AppContext = Depends(context),
) -> dict[str, Any]:
    if not project_id.strip():
        raise ApiError(400, "PROJECT_REQUIRED", "查询提示词库必须指定项目")
    items = _history(ctx).project_library(project_id.strip())
    return {"items": items, "total": len(items)}


@router.post("/import")
async def import_prompts(
    payload: PromptLibraryImportRequest,
    ctx: AppContext = Depends(context),
) -> dict[str, Any]:
    project_id = payload.project_id.strip()
    if not project_id:
        raise ApiError(400, "PROJECT_REQUIRED", "导入提示词必须指定项目")
    result = _history(ctx).import_library(
        project_id,
        payload.items,
        source_name=payload.source_name,
    )
    ctx.repo.add_event(
        "prompt_library.imported",
        f"提示词库导入 {result['imported']} 条，跳过 {result['skipped']} 条",
        actor="user",
        details={"project_id": project_id, **result},
    )
    return {"ok": not result["errors"], "projectId": project_id, **result}


@router.get("/export")
async def export_prompts(
    project_id: str = Query(alias="project_id"),
    ctx: AppContext = Depends(context),
) -> Response:
    project_id = project_id.strip()
    if not project_id:
        raise ApiError(400, "PROJECT_REQUIRED", "导出提示词必须指定项目")
    project_rows = ctx.ledger.project_stats()
    project = next(
        (row for row in project_rows if str(row.get("project_id") or "") == project_id),
        {},
    )
    project_name = str(project.get("project_name") or project_id)
    payload = {
        "schema": "solo-auto/prompt-library/1",
        "project": {"id": project_id, "name": project_name},
        "exportedAt": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "items": _history(ctx).project_library(project_id),
    }
    stamp = datetime.now().strftime("%Y%m%d")
    filename = f"prompt-library-{project_id[:12]}-{stamp}.json"
    return Response(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/dedupe")
async def dedupe_prompts(
    payload: PromptDedupeRequest,
    ctx: AppContext = Depends(context),
) -> dict[str, Any]:
    project_id = payload.project_id.strip()
    engineering_id = payload.engineering_id.strip()
    exclude_task_id = payload.exclude_task_id.strip()
    task_id = ""
    if payload.task_root.strip():
        task_root = str(Path(payload.task_root).expanduser().resolve())
        task = ctx.repo.query_one("SELECT * FROM tasks WHERE task_root = ?", (task_root,))
        if not task:
            raise ApiError(404, "TASK_NOT_FOUND", f"工作台找不到任务目录：{task_root}")
        task_id = str(task["id"])
        project_id = str(task.get("project_id") or "")
        engineering_id = str(task.get("engineering_id") or "")
        exclude_task_id = task_id
    if not project_id:
        raise ApiError(400, "PROJECT_REQUIRED", "查询提示词去重必须指定项目或任务目录")
    if not engineering_id:
        raise ApiError(400, "ENGINEERING_REQUIRED", "查询提示词去重必须指定工程")
    result = _history(ctx).dedupe(
        project_id=project_id,
        engineering_id=engineering_id,
        candidate_text=payload.text,
        exclude_task_id=exclude_task_id,
        limit=payload.limit,
    )
    return {"ok": True, "taskId": task_id, **result}
