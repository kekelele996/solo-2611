"""v3 dispatch-center routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request

from solo_auto.api.dependencies import context
from solo_auto.api.types import (
    DispatchInterruptRequest,
    DispatchLimitsRequest,
    DispatchMoveRequest,
    DispatchPlanRequest,
    DispatchPoliciesRequest,
    DispatchQueueRequest,
    DispatchRecoverRequest,
    DispatchStateRequest,
)
from solo_auto.context import AppContext
from solo_auto.errors import ApiError


async def require_v3_engine(request: Request, ctx: AppContext = Depends(context)) -> None:
    if request.method in {"POST", "PUT", "PATCH", "DELETE"} and ctx.dispatcher.config.engine == "v2":
        raise ApiError(409, "DISPATCH_ENGINE_V2_READONLY", "当前为 v2 调度引擎，调度写接口只读")


router = APIRouter(prefix="/api/dispatch", dependencies=[Depends(require_v3_engine)])


@router.get("")
async def snapshot(ctx: AppContext = Depends(context)) -> dict[str, Any]:
    return ctx.dispatcher.snapshot()


@router.post("/state")
async def state(payload: DispatchStateRequest, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    if payload.desired == "running":
        report = await ctx.preflight.run()
        if not report.get("ok"):
            raise ApiError(409, "PREFLIGHT_FAILED", "调度启动预检未通过", detail=report)
        ctx.autopilot.paused = False
        plan = ctx.intake.plan
        if plan and plan.get("status") == "paused":
            await ctx.intake.resume()
    elif payload.desired == "paused":
        ctx.autopilot.paused = True
        if ctx.intake.plan and ctx.intake.plan.get("status") == "running":
            await ctx.intake.pause("调度页已暂停")
    elif ctx.intake.plan and ctx.intake.plan.get("status") == "running":
        await ctx.intake.pause("调度排空后停止")
    return await ctx.dispatcher.set_desired_state(payload.desired)


@router.post("/interrupt")
async def interrupt(payload: DispatchInterruptRequest, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    await ctx.dispatcher.set_desired_state("paused", actor="user")
    result = await ctx.autopilot.cancel(payload.task_ids or None, kill_containers=payload.kill_containers)
    return {"ok": True, **result}


@router.patch("/limits")
async def limits(payload: DispatchLimitsRequest, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    values = payload.model_dump(exclude_none=True)
    try:
        return await ctx.dispatcher.update_limits(values)
    except ValueError as exc:
        raise ApiError(422, "DISPATCH_LIMIT_INVALID", str(exc)) from exc


@router.patch("/policies")
async def policies(payload: DispatchPoliciesRequest, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    try:
        return await ctx.dispatcher.update_policies(payload.model_dump(exclude_none=True))
    except ValueError as exc:
        raise ApiError(422, "DISPATCH_POLICY_INVALID", str(exc)) from exc


@router.get("/explain")
async def explain(task_id: str = "", ctx: AppContext = Depends(context)) -> dict[str, Any]:
    try:
        return ctx.dispatcher.explain(task_id)
    except KeyError as exc:
        raise ApiError(404, "DISPATCH_ITEM_NOT_FOUND", "调度队列中没有该任务") from exc


@router.get("/upcoming")
async def upcoming(limit: int = Query(default=10, ge=1, le=50), ctx: AppContext = Depends(context)) -> dict[str, Any]:
    return {"items": ctx.dispatcher.upcoming(limit)}


@router.get("/throughput")
async def throughput(hours: int = Query(default=24, ge=1, le=720), ctx: AppContext = Depends(context)) -> dict[str, Any]:
    return ctx.dispatcher.throughput.summary(hours)


@router.get("/capacity")
async def capacity(hours: int = Query(default=24, ge=1, le=168), ctx: AppContext = Depends(context)) -> dict[str, Any]:
    return ctx.dispatcher.throughput.capacity(hours)


@router.get("/log")
async def dispatch_log(
    after_seq: int = Query(default=0, ge=0),
    family: str = "",
    level: str = "",
    task_id: str = "",
    limit: int = Query(default=200, ge=1, le=1000),
    ctx: AppContext = Depends(context),
) -> dict[str, Any]:
    return ctx.dispatcher.logs(
        after_seq=after_seq,
        family=family,
        level=level,
        task_id=task_id,
        limit=limit,
    )


@router.get("/holds")
async def holds(ctx: AppContext = Depends(context)) -> dict[str, Any]:
    return ctx.dispatcher.holds.state()


@router.post("/holds/{hold_id}/probe")
async def probe_hold(hold_id: str, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    result = await ctx.dispatcher.probe_hold(hold_id)
    return {"source": result.source, "ok": result.ok, "message": result.message, "detail": result.detail}


@router.post("/holds/{hold_id}/release")
async def release_hold(hold_id: str, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    hold = ctx.dispatcher.release_hold(hold_id)
    if hold is None:
        raise ApiError(404, "DISPATCH_HOLD_NOT_FOUND", "挂起不存在或已解除")
    return {"ok": True, "source": hold.source, "snapshot": ctx.dispatcher.snapshot()}


@router.post("/holds/{hold_id}/convert")
async def convert_hold(hold_id: str, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    ctx.dispatcher.release_hold(hold_id, actor="user")
    return await ctx.dispatcher.set_desired_state("paused", actor="user")


@router.post("/probes/{source}")
async def probe(source: str, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    result = await ctx.dispatcher.probe_source(source)
    return {"source": result.source, "ok": result.ok, "message": result.message, "detail": result.detail}


@router.post("/recover")
async def recover(payload: DispatchRecoverRequest, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    return ctx.dispatcher.recover(since_minutes=payload.since_minutes).view()


@router.post("/queue")
async def enqueue(payload: DispatchQueueRequest, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    added: list[str] = []
    skipped: list[dict[str, str]] = []
    engineering_ids = {
        str(item.engineering_id)
        for item in ctx.dispatcher.queue.all_items(include_terminal=False)
        if item.engineering_id
    }
    for task_id in dict.fromkeys(payload.task_ids):
        task = ctx.repo.get_task(task_id)
        if task is None:
            skipped.append({"task_id": task_id, "reason": "任务不存在"})
            continue
        engineering_id = str(task.get("engineering_id") or "")
        if engineering_id and engineering_id in engineering_ids:
            skipped.append({"task_id": task_id, "reason": "同一工程已在队列中"})
            continue
        ctx.dispatcher.queue.enqueue(
            task_id,
            project_id=str(task.get("project_id") or ""),
            engineering_id=engineering_id,
            front=payload.front,
        )
        added.append(task_id)
        if engineering_id:
            engineering_ids.add(engineering_id)
    snapshot = ctx.dispatcher.snapshot()
    snapshot["added"] = added
    snapshot["skipped"] = skipped
    return snapshot


@router.post("/queue/clear-finished")
async def clear_finished(ctx: AppContext = Depends(context)) -> dict[str, Any]:
    removed = ctx.dispatcher.queue.clear_finished()
    return {"removed": removed, "snapshot": ctx.dispatcher.snapshot()}


@router.post("/queue/{task_id}/pin")
async def pin(task_id: str, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    item = ctx.dispatcher.queue.get(task_id)
    if item is None:
        raise ApiError(404, "DISPATCH_ITEM_NOT_FOUND", "任务不在调度队列")
    ctx.dispatcher.queue.update(task_id, priority=max(100, item.priority + 100), position=-1000.0)
    return {"ok": True, "snapshot": ctx.dispatcher.snapshot()}


@router.post("/queue/{task_id}/move")
async def move(task_id: str, payload: DispatchMoveRequest, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    ordered = ctx.dispatcher.queue.ordered(include_unavailable=True)
    index = next((number for number, item in enumerate(ordered) if item.task_id == task_id), -1)
    if index < 0:
        raise ApiError(404, "DISPATCH_ITEM_NOT_FOUND", "任务不在调度队列")
    target = 0 if payload.direction == "top" else max(0, index - 1 if payload.direction == "up" else index + 1)
    if target != index and target < len(ordered):
        ordered.insert(target, ordered.pop(index))
        for position, item in enumerate(ordered):
            ctx.dispatcher.queue.update(item.task_id, position=float(position * 10))
    return {"ok": True, "snapshot": ctx.dispatcher.snapshot()}


@router.post("/queue/{task_id}/retry")
async def retry(task_id: str, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    item = ctx.dispatcher.queue.get(task_id)
    if item is None:
        raise ApiError(404, "DISPATCH_ITEM_NOT_FOUND", "任务不在调度队列")
    from_stage = item.stage if item.stage not in {"pending", "running"} else ""
    try:
        await ctx.autopilot.retry(task_id, from_stage=from_stage)
    except ApiError as exc:
        if exc.code not in {"AUTOPILOT_JOB_NOT_FOUND"}:
            raise
    ctx.dispatcher.queue.update(
        task_id,
        status="queued",
        not_before="",
        wait_code="",
        wait_text="",
        wait_until="",
    )
    return {"ok": True, "snapshot": ctx.dispatcher.snapshot()}


@router.post("/queue/{task_id}/quarantine")
async def quarantine(task_id: str, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    item = ctx.dispatcher.queue.update(
        task_id,
        status="quarantined",
        wait_code="quarantined",
        wait_text="人工隔离",
        wait_until="",
    )
    if item is None:
        raise ApiError(404, "DISPATCH_ITEM_NOT_FOUND", "任务不在调度队列")
    return {"ok": True, "snapshot": ctx.dispatcher.snapshot()}


@router.post("/queue/{task_id}/release")
async def release(task_id: str, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    item = ctx.dispatcher.queue.remove(task_id)
    if item is None:
        raise ApiError(404, "DISPATCH_ITEM_NOT_FOUND", "任务不在调度队列")
    return {"ok": True, "snapshot": ctx.dispatcher.snapshot()}


@router.post("/queue/{task_id}/keep")
async def keep(task_id: str, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    item = ctx.dispatcher.queue.get(task_id)
    if item is None:
        raise ApiError(404, "DISPATCH_ITEM_NOT_FOUND", "任务不在调度队列")
    ctx.dispatcher.queue.update(task_id, keep=not item.keep)
    return {"ok": True, "keep": not item.keep, "snapshot": ctx.dispatcher.snapshot()}


@router.get("/plan")
async def get_plan(ctx: AppContext = Depends(context)) -> dict[str, Any]:
    return {"plan": ctx.dispatcher.plan, "intake": ctx.intake.snapshot()}


@router.put("/plan")
async def put_plan(payload: DispatchPlanRequest, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    if payload.mode == "continuous" and payload.daily_claim_cap < 1:
        raise ApiError(422, "DAILY_CAP_REQUIRED", "持续生产必须设置每日领取上限")
    if payload.mode == "target" and payload.target < 1:
        raise ApiError(422, "TARGET_REQUIRED", "目标模式必须设置目标题数")
    ctx.dispatcher.plan = payload.model_dump()
    ctx.dispatcher._save_state()
    ctx.settings.dispatch.refill.mode = payload.mode
    ctx.settings.dispatch.refill.target = payload.target
    ctx.settings.dispatch.refill.daily_claim_cap = payload.daily_claim_cap
    ctx.settings.dispatch.refill.allow_reuse = payload.allow_reuse
    ctx.settings.dispatch.refill.engineering_usage_limit = payload.engineering_usage_limit
    ctx.save_settings()
    result: dict[str, Any] = {"ok": True, "plan": ctx.dispatcher.plan}
    if len(payload.project_ids) == 1 and payload.mode == "target":
        report = await ctx.preflight.run()
        if not report.get("ok"):
            raise ApiError(409, "PREFLIGHT_FAILED", "生产目标预检未通过", detail=report)
        result["intake"] = await ctx.intake.create(
            project_id=payload.project_ids[0],
            target=payload.target,
            filters=payload.filters,
            exclude_delivered=not payload.allow_reuse,
            max_uses=payload.engineering_usage_limit,
        )
    return result


@router.post("/plan/stop")
async def stop_plan(release_unstarted: bool = True, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    result = await ctx.intake.stop(release_unstarted=release_unstarted)
    ctx.dispatcher.plan = {**ctx.dispatcher.plan, "status": "stopped"}
    ctx.dispatcher._save_state()
    return result


@router.get("/guard")
async def guard(ctx: AppContext = Depends(context)) -> dict[str, Any]:
    hits = [hit.view() for hit in ctx.dispatcher.guards.scan()]
    return {"mode": ctx.dispatcher.config.guard.mode, "items": hits}


@router.post("/guard/{hit_id}/apply")
async def apply_guard(hit_id: str, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    hit = ctx.dispatcher.guards.apply(hit_id)
    if hit is None:
        raise ApiError(404, "GUARD_HIT_NOT_FOUND", "守护命中不存在或已忽略")
    return {"ok": True, "hit": hit.view(), "snapshot": ctx.dispatcher.snapshot()}


@router.post("/guard/{hit_id}/dismiss")
async def dismiss_guard(hit_id: str, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    hit = ctx.dispatcher.guards.dismiss(hit_id)
    if hit is None:
        raise ApiError(404, "GUARD_HIT_NOT_FOUND", "守护命中不存在")
    return {"ok": True, "hit": hit.view()}
