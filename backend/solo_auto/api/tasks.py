"""Task list, lifecycle, prompt, snapshot and run routes."""

from __future__ import annotations

import asyncio
import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from fastapi import APIRouter, Depends, Query

from solo_auto.api.dependencies import context
from solo_auto.api.types import (
    AbortRequest,
    CollectRequest,
    PromptDraftRequest,
    PromptInstallRequest,
    ReopenRequest,
    ResetRequest,
    RunRequest,
    RunStopRequest,
)
from solo_auto.context import AppContext
from solo_auto.errors import ApiError
from solo_auto.pipeline.platform_result import platform_status
from solo_auto.pipeline.steps import evaluate_steps
from solo_auto.store.state import load_state

router = APIRouter(prefix="/api")


@router.get("/tasks")
async def list_tasks(
    status: str = "",
    q: str = "",
    project: str = "",
    date: str = "",
    compact: bool = False,
    view: str = "",
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    ctx: AppContext = Depends(context),
) -> dict[str, Any]:
    positions = {entry.task_id: index for index, entry in enumerate(ctx.scheduler.queue, 1)}
    rows, total = ctx.repo.list_tasks(
        status=status if status and status != "queued" else None,
        task_ids=list(positions) if status == "queued" else None,
        view=view, failed_ids=[job.task_id for job in ctx.autopilot.jobs.values() if job.stage == "failed"] if view == "failed" else None,
        q=q or None, project=project or None, date=date or None, page=page, page_size=page_size,
    )
    for row in rows:
        row["platform_status"] = platform_status(row["task_root"])
        job = ctx.autopilot.jobs.get(row["id"])
        row["stage_started_at"] = job.stage_started_at if job and job.active else None
        row["queue_position"] = positions.get(row["id"])
        if row["queue_position"]:
            row["job_status"] = "queued"
        if not compact:
            row["run"] = ctx.scheduler.run_status(row["id"], Path(row["task_root"]))
    return {"items": rows, "total": total, "page": page, "page_size": page_size}


@router.get("/task-counts")
async def task_counts(ctx: AppContext = Depends(context)) -> dict[str, int]:
    counts = {"passed": 0, "submitted": 0, "exported": 0, "discarded": 0, "retrying": 0}
    aggregate = ctx.repo.query_one("""SELECT
        COUNT(CASE WHEN job_status IN ('authored','prompt_ok','ready','ran','recorded','reviewed') THEN 1 END) AS waiting,
        ROUND(AVG(CASE WHEN julianday(exported_at) >= julianday(created_at)
            THEN (julianday(exported_at) - julianday(created_at))*86400 END)) AS average_seconds FROM tasks""") or {}
    counts["waiting"] = int(aggregate.get("waiting") or 0)
    counts["average_seconds"] = int(aggregate["average_seconds"]) if aggregate.get("average_seconds") is not None else -1
    retrying = ctx.repo.query_one("SELECT COUNT(*) AS n FROM dispatch_items WHERE status='backoff'")
    counts["retrying"] = int((retrying or {}).get("n", 0))
    for row in ctx.repo.query("SELECT task_root, exported_at FROM tasks"):
        status = platform_status(row["task_root"])
        if status == "qc_passed":
            counts["passed"] += 1
        elif status in {"discarded", "qc_discarded"}:
            counts["discarded"] += 1
        elif status in {"submitted", "qc_running", "qc_waiting", "fix_submitted"}:
            counts["submitted"] += 1
        elif status in {"", "dry_run"} and row.get("exported_at"):
            counts["exported"] += 1
    return counts


@router.get("/tasks/{task_id}")
async def task_detail(task_id: str, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    task = _task(ctx, task_id)
    root = Path(task["task_root"])
    envelope = load_state(root)
    evaluation = evaluate_steps(root, duplicate_sessions=ctx.repo.session_counts())
    task["steps"] = [step.as_dict() for step in evaluation.steps]
    task["state"] = envelope.raw
    task["compatible"] = envelope.compatible
    task["compatibility_error"] = envelope.error
    task["complete"] = evaluation.complete
    task["missing"] = evaluation.missing
    task["run"] = ctx.scheduler.run_status(task_id, root)
    task["queue_position"] = ctx.scheduler.queue_position(task_id)
    meta = _task_meta(ctx, root)
    task["source"] = meta
    task["platform_status"] = platform_status(str(root))
    if binding := ctx.routing.row(task_id=task_id):
        receipt = ctx.routing.receipt(root)
        waiting = receipt.get("status") == "quota_deferred"
        task["account_dispatch"] = {
            "role": binding["owner"], "project": binding["meta"]["projectName"],
            "reason": binding["drop_reason"] or (receipt.get("message") if waiting or receipt.get("status") == "quota_stopped" else "") or "",
            "retry_at": str(receipt.get("retryAt") or "") if waiting and not binding["dropped_at"] else "",
            "stopped": bool(binding["dropped_at"]),
        }
    timings = ctx.repo.query("SELECT id, details FROM events WHERE task_id=? AND action='autopilot.stage_duration' ORDER BY id DESC", (task_id,))
    task["stage_history"] = ([{"id": row["id"], **json.loads(row["details"])} for row in timings]
                             if timings else ctx.repo.autopilot_stages(task_id))
    return task


@router.get("/tasks/{task_id}/steps")
async def task_steps(task_id: str, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    task = _task(ctx, task_id)
    evaluation = evaluate_steps(Path(task["task_root"]), duplicate_sessions=ctx.repo.session_counts())
    return {"items": [step.as_dict() for step in evaluation.steps], "complete": evaluation.complete}


@router.get("/tasks/{task_id}/events")
async def task_events(task_id: str, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    _task(ctx, task_id)
    return {"items": ctx.repo.events(task_id=task_id, limit=500)}


@router.post("/tasks/{task_id}/abort")
async def abort_task(
    task_id: str,
    payload: AbortRequest,
    ctx: AppContext = Depends(context),
) -> dict[str, Any]:
    task = _task(ctx, task_id)
    reason = payload.reason.strip()
    if not reason:
        raise ApiError(422, "ABORT_REASON_REQUIRED", "放弃任务必须填写原因")
    envelope = load_state(Path(task["task_root"]))
    managed = envelope.state.sessionRun if envelope.state else None
    cleanup = await ctx.gateway.call(
        (
            ["abort", "--task-root", str(task["task_root"]), "--reason", reason, "--by", "human"]
            if managed and managed.status not in {"accepted", "aborted"}
            else ["cleanup", "--task-root", str(task["task_root"])]
        ),
        timeout=120,
        task_id=task_id,
    )
    if cleanup.code != 0:
        raise ApiError(
            500,
            "CLEANUP_FAILED",
            cleanup.err.strip() or "容器清理失败，任务未标记放弃",
            detail={"stdout": cleanup.out, "stderr": cleanup.err},
        )
    ctx.repo.set_job_status(task_id, "aborted", notes=reason)
    ctx.ledger.mark_outcome(task_id, "void")
    await ctx.intake.terminal_session(task_id, reason="manual_abort")
    ctx.repo.add_event(
        "task.aborted",
        reason,
        level="warn",
        actor="user",
        task_id=task_id,
        details={"cleanup": cleanup.data or {}},
    )
    return {
        "ok": True,
        "task_id": task_id,
        "job_status": "aborted",
        "cleanup_required": False,
        "task_root": task["task_root"],
    }


@router.post("/tasks/{task_id}/archive")
async def archive_task(task_id: str, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    task = _task(ctx, task_id)
    if task.get("job_status") != "exported":
        raise ApiError(409, "TASK_NOT_EXPORTED", "只有已导出任务可以归档")
    root = Path(task["task_root"])
    cleanup = await ctx.gateway.call(["cleanup", "--task-root", str(root)], timeout=120, task_id=task_id)
    if cleanup.code != 0:
        raise ApiError(
            500,
            "CLEANUP_FAILED",
            cleanup.err.strip() or "容器清理失败，任务未归档",
            detail={"stdout": cleanup.out, "stderr": cleanup.err},
        )
    meta = _task_meta(ctx, root)
    release_error = ""
    if meta.get("projectId") and meta.get("engineeringId") and meta.get("claimId"):
        try:
            await ctx.release_profile_claim(meta)
            ctx.repo.execute(
                "UPDATE claims SET released_at = ? WHERE id = ?",
                (datetime.now().astimezone().isoformat(), meta["claimId"]),
            )
        except Exception as exc:
            release_error = str(exc)
    ctx.repo.set_job_status(
        task_id, "archived", notes="已归档" + (f"；归还失败：{release_error}" if release_error else "")
    )
    ctx.repo.add_event(
        "task.archived",
        "任务已归档并清理容器" + ("；工程台归还未成功" if release_error else "，工程已归还"),
        level="warn" if release_error else "info",
        actor="user",
        task_id=task_id,
        details={"cleanup": cleanup.data or {}, "release_error": release_error},
    )
    return {"ok": True, "task_id": task_id, "release_error": release_error}


@router.delete("/tasks/{task_id}")
async def delete_task(
    task_id: str,
    dry_run: bool = True,
    ctx: AppContext = Depends(context),
) -> dict[str, Any]:
    task = _task(ctx, task_id)
    if task.get("job_status") != "aborted":
        raise ApiError(409, "TASK_NOT_ABORTED", "只有已放弃任务允许删除")
    root = Path(task["task_root"]).resolve()
    tasks_dir = ctx.settings.tasks_dir.resolve()
    if not root.is_relative_to(tasks_dir) or root == tasks_dir:
        raise ApiError(400, "DELETE_PATH_INVALID", "任务路径不在配置的任务目录内")
    paths = [str(root)]
    meta = _task_meta(ctx, root)
    if meta.get("stagingPath"):
        paths.append(str(meta["stagingPath"]))
    payload = {
        "dry_run": dry_run,
        "paths": paths,
        "recoverable": False,
        "warning": "删除后无法从控制台恢复，请先确认证据已不需要。",
    }
    if not dry_run:
        if root.is_dir():
            shutil.rmtree(root)
        staging_path = Path(str(meta.get("stagingPath") or ""))
        if staging_path.is_dir() and staging_path.is_relative_to(ctx.paths.staging.resolve()):
            shutil.rmtree(staging_path)
        ctx.repo.set_job_status(task_id, "archived", notes="已删除任务目录")
        ctx.ledger.mark_purged([task_id])
        ctx.repo.add_event("task.deleted", "已放弃任务的本地目录被删除", level="warn", actor="user", task_id=task_id)
    return payload


@router.post("/tasks/{task_id}/forget")
async def forget_task(task_id: str, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    task = _task(ctx, task_id)
    if task.get("presence") != "missing":
        raise ApiError(409, "TASK_DIRECTORY_PRESENT", "只有目录已不存在的任务才能移除记录")
    ctx.autopilot.forget(task_id)
    ctx.repo.execute("UPDATE claims SET task_id=NULL WHERE task_id=?", (task_id,))
    ctx.repo.execute("DELETE FROM tasks WHERE id=?", (task_id,))
    ctx.repo.add_event(
        "task.forgotten",
        f"{task['task_name']} 的目录已不存在，记录已移除",
        level="warn",
        actor="user",
        task_id=task_id,
    )
    ctx.bus.publish("task.updated", {"forgotten": task_id})
    return {"ok": True, "task_id": task_id}


@router.get("/tasks/{task_id}/prompt")
async def get_prompt(task_id: str, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    return ctx.prompts.get(task_id)


@router.put("/tasks/{task_id}/prompt/draft")
async def save_prompt_draft(
    task_id: str,
    payload: PromptDraftRequest,
    ctx: AppContext = Depends(context),
) -> dict[str, Any]:
    return ctx.prompts.save_draft(task_id, payload.text)


@router.post("/tasks/{task_id}/prompt/preview")
async def preview_prompt(
    task_id: str,
    payload: PromptDraftRequest,
    ctx: AppContext = Depends(context),
) -> dict[str, Any]:
    _task(ctx, task_id)
    return ctx.prompts.preview(payload.text)


@router.post("/tasks/{task_id}/prompt/install")
async def install_prompt(
    task_id: str,
    payload: PromptInstallRequest,
    ctx: AppContext = Depends(context),
) -> dict[str, Any]:
    return await ctx.prompts.install(
        task_id,
        task_type=payload.task_type,
        difficulty=payload.difficulty,
        languages=payload.languages,
        repro_level=payload.repro_level,
        domain=payload.domain,
        sub_direction=payload.sub_direction,
        allow_over_170=payload.allow_over_170,
    )


@router.post("/tasks/{task_id}/prompt/reopen")
async def reopen_prompt(
    task_id: str,
    payload: ReopenRequest,
    ctx: AppContext = Depends(context),
) -> dict[str, Any]:
    return ctx.prompts.reopen(task_id, payload.reason)


@router.get("/tasks/{task_id}/reviews")
async def reviews(task_id: str, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    return ctx.prompts.reviews(task_id)


@router.put("/tasks/{task_id}/reviews/{kind}")
async def save_review(
    task_id: str,
    kind: str,
    payload: dict[str, Any],
    ctx: AppContext = Depends(context),
) -> dict[str, Any]:
    return ctx.prompts.save_review(task_id, kind, payload)


@router.post("/tasks/{task_id}/repo")
async def push_repo(task_id: str, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    task = _task(ctx, task_id)
    result = await ctx.gateway.call(
        ["repo", "--task-root", task["task_root"]],
        timeout=900,
        task_id=task_id,
    )
    if result.code != 0:
        raise ApiError(
            500,
            "REPO_PUSH_FAILED",
            result.err.strip() or "GitHub 仓库推送失败",
            detail={"stdout": result.out, "stderr": result.err, "exit_code": result.code},
        )
    await ctx.reconciler.run()
    data = result.data or {}
    ctx.repo.add_event("repo.pushed", f"远端快照已更新：{data.get('repo', '')}", actor="user", task_id=task_id)
    return {
        "ok": True,
        "initial_sha": "",
        "permalink": data.get("初始环境快照", ""),
        "artifact_permalink": data.get("产物快照", ""),
        "error": "",
        **data,
    }


@router.get("/tasks/{task_id}/snapshots")
async def snapshots(task_id: str, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    task = _task(ctx, task_id)
    envelope = load_state(Path(task["task_root"]))
    state = envelope.state
    if state is None:
        raise ApiError(409, "TASK_STATE_INCOMPATIBLE", envelope.error or "任务状态不可读")
    initial = state.initialSha
    artifact = state.artifact.sha if state.artifact else ""
    ancestor = False
    if initial and artifact:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            state.workspace,
            "merge-base",
            "--is-ancestor",
            initial,
            artifact,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        ancestor = proc.returncode == 0
    changes = None
    if initial and artifact and ancestor:
        proc = await asyncio.create_subprocess_exec("git", "-C", state.workspace, "diff", "--name-status", "-z", "--find-renames", initial, artifact, "--", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        raw, _ = await proc.communicate()
        if proc.returncode == 0:
            changes = {"added": 0, "modified": 0, "deleted": 0, "renamed": 0}
            records = iter(raw.split(b"\0"))
            for record in records:
                if not record:
                    break
                kind = {b"A": "added", b"D": "deleted", b"R": "renamed"}.get(record[:1], "modified")
                changes[kind] += 1
                next(records)
                if record.startswith(b"R"):
                    next(records)
    base = state.remote.web.rstrip("/")
    return {
        "initial": {
            "sha": initial,
            "permalink": f"{base}/commit/{initial}" if base and initial else "",
        },
        "artifact": {
            "sha": artifact,
            "permalink": f"{base}/commit/{artifact}" if base and artifact else "",
            "short_stat": state.artifact.shortStat if state.artifact else "",
        },
        "ancestor_ok": ancestor,
        "changes": changes,
    }


@router.post("/tasks/{task_id}/run")
async def enqueue_run(
    task_id: str,
    payload: RunRequest,
    ctx: AppContext = Depends(context),
) -> dict[str, Any]:
    return await ctx.scheduler.enqueue(task_id, pinned=payload.pinned)


@router.get("/tasks/{task_id}/run")
async def run_status(task_id: str, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    task = _task(ctx, task_id)
    return ctx.scheduler.run_status(task_id, Path(str(task["task_root"])))


@router.post("/tasks/{task_id}/run/stop")
async def stop_run(
    task_id: str,
    payload: RunStopRequest,
    ctx: AppContext = Depends(context),
) -> dict[str, Any]:
    return await ctx.scheduler.stop_task(task_id, force=payload.force)


@router.get("/tasks/{task_id}/run/log")
async def run_log(
    task_id: str,
    lines: int = Query(default=200, ge=1, le=1000),
    ctx: AppContext = Depends(context),
) -> Any:
    _task(ctx, task_id)
    task = _task(ctx, task_id)
    status = ctx.scheduler.run_status(task_id, Path(str(task["task_root"])))
    text = "\n".join(str(status.get("log_tail") or "").splitlines()[-lines:])
    from fastapi.responses import PlainTextResponse

    return PlainTextResponse(text)


@router.post("/tasks/{task_id}/run/interactive")
async def interactive_run(task_id: str, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    task = _task(ctx, task_id)
    ctx.routing.audit(task_id, Path(task["task_root"]))
    result = await ctx.gateway.call(
        ["interactive", "--task-root", task["task_root"]],
        timeout=1800,
        task_id=task_id,
    )
    if result.code != 0:
        raise ApiError(
            500,
            "INTERACTIVE_FAILED",
            result.err.strip() or "交互式启动命令生成失败",
            detail={"stdout": result.out, "stderr": result.err},
        )
    envelope = load_state(Path(task["task_root"]))
    container_hint = envelope.state.pendingContainer if envelope.state else ""
    await ctx.reconciler.run()
    return {"command": result.out, "container_hint": container_hint}


@router.post("/tasks/{task_id}/terminal-view")
async def open_terminal_view(task_id: str, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    task = _task(ctx, task_id)
    result = await ctx.gateway.call(
        ["terminal-view", "--task-root", task["task_root"]],
        timeout=60,
        task_id=task_id,
    )
    if result.code != 0:
        raise ApiError(
            409,
            "TERMINAL_VIEW_FAILED",
            result.err.strip() or "当前任务没有可查看的工作终端",
            detail={"stdout": result.out, "stderr": result.err},
        )
    return result.data or {"ok": True}


@router.post("/terminals/open-all")
async def open_all_terminal_views(ctx: AppContext = Depends(context)) -> dict[str, Any]:
    result = await ctx.gateway.call(["terminal-view-all"], timeout=120)
    if result.code != 0:
        raise ApiError(
            409,
            "TERMINAL_VIEW_ALL_FAILED",
            result.err.strip() or "没有可查看的工作终端",
            detail={"stdout": result.out, "stderr": result.err},
        )
    return result.data or {"ok": True}


@router.post("/tasks/{task_id}/run/collect")
async def collect_run(
    task_id: str,
    payload: CollectRequest,
    ctx: AppContext = Depends(context),
) -> dict[str, Any]:
    task = _task(ctx, task_id)
    argv = ["collect", "--task-root", task["task_root"]]
    if payload.container:
        argv.extend(["--container", payload.container])
    result = await ctx.gateway.call(argv, timeout=1800, task_id=task_id)
    if result.code not in {0, 2}:
        raise ApiError(
            500,
            "COLLECT_FAILED",
            result.err.strip() or "交互会话收集失败",
            detail={"stdout": result.out, "stderr": result.err},
        )
    await ctx.reconciler.run()
    return {
        "ok": result.code == 0,
        "data": result.data or {},
        "errors": (result.data or {}).get("errors") or [],
        "stderr": result.err,
    }


@router.post("/tasks/{task_id}/reset")
async def reset_run(
    task_id: str,
    payload: ResetRequest,
    ctx: AppContext = Depends(context),
) -> dict[str, Any]:
    task = _task(ctx, task_id)
    if payload.confirm != task["task_name"]:
        raise ApiError(
            422,
            "RESET_CONFIRM_REQUIRED",
            f"必须输入任务名 {task['task_name']} 才能重置",
            detail={"task_name": task["task_name"], "effect": "丢弃初始快照之后的全部 workspace 改动"},
        )
    result = await ctx.gateway.call(
        ["reset", "--task-root", task["task_root"], "--yes"],
        timeout=300,
        task_id=task_id,
    )
    if result.code != 0:
        raise ApiError(500, "RESET_FAILED", result.err.strip() or "任务重置失败")
    await ctx.reconciler.run()
    return {"ok": True, "data": result.data or {}}


@router.get("/queue")
async def queue(ctx: AppContext = Depends(context)) -> dict[str, Any]:
    return ctx.scheduler.queue_snapshot()


def _task(ctx: AppContext, task_id: str) -> dict[str, Any]:
    task = ctx.repo.get_task(task_id)
    if not task:
        raise ApiError(404, "TASK_NOT_FOUND", f"任务不存在：{task_id}")
    return task


def _task_meta(ctx: AppContext, root: Path) -> dict[str, Any]:
    for path in ctx.paths.staging.glob("*/meta.json"):
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(meta, dict) and str(meta.get("taskRoot") or "") == str(root):
            return meta
    try:
        data = json.loads((ctx.paths.run / "task-meta" / f"{root.name}.json").read_text(encoding="utf-8"))
        return cast(dict[str, Any], data) if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}
