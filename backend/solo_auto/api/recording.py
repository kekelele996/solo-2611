"""Recording plan and capture routes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse

from solo_auto.api.dependencies import context
from solo_auto.api.types import (
    ChromeStartRequest,
    PrivacyRecheckRequest,
    RecordDeleteRequest,
    RecordPlanGenerateRequest,
    WindowStartRequest,
)
from solo_auto.context import AppContext
from solo_auto.errors import ApiError
from solo_auto.store.state import load_state

router = APIRouter(prefix="/api")


@router.get("/recording/windows")
async def windows(ctx: AppContext = Depends(context)) -> dict[str, Any]:
    return {"windows": await ctx.recording.windows()}


@router.get("/recording/status")
async def status(ctx: AppContext = Depends(context)) -> dict[str, Any]:
    return await ctx.recording.status()


@router.post("/recordings/privacy-recheck")
async def privacy_recheck(
    payload: PrivacyRecheckRequest,
    ctx: AppContext = Depends(context),
) -> dict[str, Any]:
    items = [ctx.recording.privacy_recheck(task_id) for task_id in dict.fromkeys(payload.task_ids)]
    return {
        "items": items,
        "passed": sum(item["status"] == "passed" for item in items),
        "unchecked": sum(item["status"] == "unchecked" for item in items),
        "failed": sum(item["status"] in {"failed", "cannot_check"} for item in items),
    }


@router.post("/recording/stop")
async def stop(ctx: AppContext = Depends(context)) -> dict[str, Any]:
    return await ctx.recording.stop()


@router.get("/tasks/{task_id}/record/plan")
async def get_plan(task_id: str, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    return await ctx.recording.get_plan(task_id)


@router.put("/tasks/{task_id}/record/plan")
async def put_plan(task_id: str, payload: dict[str, Any], ctx: AppContext = Depends(context)) -> dict[str, Any]:
    return await ctx.recording.save_plan(task_id, payload)


@router.post("/tasks/{task_id}/record/plan/generate")
async def generate_plan(
    task_id: str,
    payload: RecordPlanGenerateRequest,
    ctx: AppContext = Depends(context),
) -> dict[str, Any]:
    task = ctx.repo.get_task(task_id)
    if not task:
        raise ApiError(404, "TASK_NOT_FOUND", f"任务不存在：{task_id}")
    argv = [
        "record",
        "plan",
        "--task-root",
        task["task_root"],
        "--mode",
        payload.mode,
    ]
    if payload.force:
        argv.append("--force")
    result = await ctx.gateway.call(argv, timeout=120, task_id=task_id)
    if result.code != 0:
        raise ApiError(
            500,
            "RECORD_PLAN_GENERATE_FAILED",
            result.err.strip() or "录屏计划生成失败",
            detail={"stdout": result.out, "stderr": result.err},
        )
    return await ctx.recording.get_plan(task_id)


@router.post("/tasks/{task_id}/record/start")
async def start(task_id: str, payload: WindowStartRequest, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    return await ctx.recording.start(task_id, window_id=payload.window_id, plan=payload.plan)


@router.get("/tasks/{task_id}/record/verify")
async def verify(task_id: str, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    return await ctx.recording.verify(task_id)


@router.get("/tasks/{task_id}/recording/video")
async def recording_video(task_id: str, ctx: AppContext = Depends(context)) -> FileResponse:
    task = ctx.repo.get_task(task_id)
    if not task:
        raise ApiError(404, "TASK_NOT_FOUND", f"任务不存在：{task_id}")
    envelope = load_state(Path(str(task["task_root"])))
    if envelope.state is None or envelope.state.recording is None or not envelope.state.recording.video:
        raise ApiError(404, "RECORDING_NOT_FOUND", "任务没有录屏")
    path = Path(envelope.state.recording.video).resolve()
    if not path.is_file():
        raise ApiError(404, "RECORDING_NOT_FOUND", "录屏文件不存在")
    return FileResponse(path, media_type="video/mp4", filename=path.name)


@router.post("/tasks/{task_id}/record/delete")
async def delete(task_id: str, payload: RecordDeleteRequest, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    return await ctx.recording.delete(task_id, confirm=payload.confirm)


@router.post("/tasks/{task_id}/record/terminal")
async def open_terminal(task_id: str, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    return await ctx.recording.open_terminal(task_id)


@router.post("/tasks/{task_id}/record/chrome-start")
async def chrome_start(
    task_id: str,
    payload: ChromeStartRequest,
    ctx: AppContext = Depends(context),
) -> dict[str, Any]:
    return await ctx.recording.chrome_start(task_id, payload.url)


@router.post("/tasks/{task_id}/record/chrome-stop")
async def chrome_stop(task_id: str, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    return await ctx.recording.chrome_stop(task_id)
