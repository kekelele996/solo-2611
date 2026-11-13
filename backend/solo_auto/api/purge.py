"""Remove tasks from the platform: preview first, trash by default."""

from __future__ import annotations

import asyncio
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends

from solo_auto.api.dependencies import context
from solo_auto.api.tasks import _task_meta
from solo_auto.api.types import PurgeRequest
from solo_auto.context import AppContext
from solo_auto.errors import ApiError
from solo_auto.pipeline.reaper import reap_and_record
from solo_auto.store.state import load_state

router = APIRouter(prefix="/api")


@router.post("/tasks/purge")
async def purge_tasks(payload: PurgeRequest, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    plans = []
    for task_id in dict.fromkeys(payload.task_ids):
        task = ctx.repo.get_task(task_id)
        if not task:
            raise ApiError(404, "TASK_NOT_FOUND", f"任务不存在：{task_id}")
        plans.append(_plan(ctx, task))
    if not payload.dry_run:
        for plan in plans:
            if plan["blockers"]:
                continue
            try:
                plan.update(await _execute(ctx, plan, permanent=payload.permanent))
            except ApiError as error:
                plan["error"] = error.message
            except OSError as error:
                plan["error"] = f"{type(error).__name__}: {error}"
        removed = [plan["task_id"] for plan in plans if plan.get("removed")]
        # 稍后再广播：让发起清理的页面先拿到结果离开，免得它被事件催着去重查已经不存在的任务
        asyncio.get_running_loop().call_later(1.0, ctx.bus.publish, "task.updated", {"purged": removed})
    return {
        "dry_run": payload.dry_run,
        "permanent": payload.permanent,
        "trash": str(ctx.paths.trash),
        "items": plans,
        "total_bytes": sum(plan["size"] for plan in plans if not plan["blockers"]),
    }


def _plan(ctx: AppContext, task: dict[str, Any]) -> dict[str, Any]:
    task_id = str(task["id"])
    root = Path(task["task_root"]).resolve()
    tasks_dir = ctx.settings.tasks_dir.resolve()
    meta = _task_meta(ctx, root)
    blockers: list[str] = []
    warnings: list[str] = []
    if task_id in ctx.scheduler.active or ctx.scheduler.queue_position(task_id) is not None:
        blockers.append("正在排队或运行，请先停止运行")
    job = ctx.autopilot.jobs.get(task_id)
    if job and job.active:
        blockers.append("自动交付进行中，请先停止")
    if root == tasks_dir or not root.is_relative_to(tasks_dir):
        blockers.append("任务路径不在配置的任务目录内")

    paths = [root] if root.exists() else []
    staging = Path(str(meta.get("stagingPath") or "/nonexistent")).resolve()
    if (
        staging.is_dir()
        and staging != ctx.paths.staging.resolve()
        and staging.is_relative_to(ctx.paths.staging.resolve())
    ):
        paths.append(staging)
    for extra in (
        ctx.paths.drafts / task_id,
        ctx.paths.logs / "autopilot" / task_id,
        ctx.paths.run / "task-meta" / f"{root.name}.json",
    ):
        if extra.exists():
            paths.append(extra)

    envelope = load_state(root) if (root / "monitor" / "state.json").is_file() else None
    state = envelope.state if envelope else None
    if task.get("exported_at") or (state and state.export and state.export.xlsx):
        warnings.append("这题已经导出过：交付表里填的轨迹和录屏路径指向这个目录，清理后这些路径会失效")
    claimed = bool(meta.get("projectId") and meta.get("engineeringId") and meta.get("claimId"))
    if claimed:
        warnings.append("会把领取的工程归还给工程台")
    if state and state.remote.web:
        warnings.append(f"GitHub 仓库不会被删除：{state.remote.web}")
    return {
        "task_id": task_id,
        "task_name": task["task_name"],
        "job_status": task["job_status"],
        "paths": [str(path) for path in paths],
        "size": sum(_size(path) for path in paths),
        "blockers": blockers,
        "warnings": warnings,
        "has_state": envelope is not None,
        "claimed": claimed,
    }


async def _execute(ctx: AppContext, plan: dict[str, Any], *, permanent: bool) -> dict[str, Any]:
    task_id = str(plan["task_id"])
    task = ctx.repo.get_task(task_id) or {}
    root = Path(str(task["task_root"])).resolve()
    if plan["has_state"]:
        # 任务整个不要了，未 collect 的交互容器也一并丢弃
        await reap_and_record(ctx.repo, root, task_id=task_id, actor="user")
        cleanup = await ctx.gateway.call(["cleanup", "--task-root", str(root), "--force"], timeout=120, task_id=task_id)
        if cleanup.code != 0:
            raise ApiError(500, "CLEANUP_FAILED", cleanup.err.strip() or "容器清理失败，任务未清理")
    release_error = ""
    if plan["claimed"]:
        meta = _task_meta(ctx, root)
        try:
            await ctx.source.release(str(meta["projectId"]), str(meta["engineeringId"]), str(meta["claimId"]))
            ctx.repo.execute(
                "UPDATE claims SET released_at = ? WHERE id = ?",
                (datetime.now().astimezone().isoformat(), meta["claimId"]),
            )
        except Exception as exc:
            release_error = str(exc)
    moved_to = ""
    sources = [Path(path) for path in plan["paths"]]
    if permanent:
        await asyncio.to_thread(_remove, sources)
    else:
        meta = await asyncio.to_thread(
            ctx.trash.move,
            task={**task, "task_id": task_id},
            sources=sources,
            release_error=release_error,
        )
        moved_to = str(ctx.paths.trash / str(meta["name"]))
    ctx.autopilot.forget(task_id)
    ctx.repo.execute("UPDATE claims SET task_id = NULL WHERE task_id = ?", (task_id,))
    ctx.repo.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    ctx.ledger.mark_purged([task_id])
    ctx.repo.add_event(
        "task.purged",
        f"{plan['task_name']} 已清理（{'永久删除' if permanent else '移入回收站'}）"
        + ("；工程台归还未成功" if release_error else ""),
        level="warn",
        actor="user",
        details={"task_id": task_id, "paths": plan["paths"], "moved_to": moved_to, "release_error": release_error},
    )
    return {"removed": True, "moved_to": moved_to, "release_error": release_error}


def _remove(sources: list[Path]) -> None:
    for source in sources:
        if source.is_dir():
            shutil.rmtree(source)
        else:
            source.unlink(missing_ok=True)


def _size(path: Path) -> int:
    if not path.is_dir():
        return path.lstat().st_size if path.exists() else 0
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file() and not item.is_symlink():
                total += item.lstat().st_size
        except OSError:
            continue
    return total
