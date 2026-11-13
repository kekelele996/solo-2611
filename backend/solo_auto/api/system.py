"""System, configuration and task-source routes."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import sqlite3
import tempfile
import zipfile
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit, urlunsplit

from fastapi import APIRouter, Depends, Query, Request, Response

from solo_auto.api.dependencies import context
from solo_auto.api.types import (
    ClaimRequest,
    ConfigPatchRequest,
    ConfigTestRequest,
    CreateTaskRequest,
    EngineeringDisableRequest,
    PruneRequest,
    RestartRequest,
    SourceLoginRequest,
)
from solo_auto.build_info import build_id
from solo_auto.context import AppContext
from solo_auto.errors import ApiError, SourceAuthError, SourceOfflineError
from solo_auto.pipeline.reaper import reap_and_record
from solo_auto.pipeline.usage import UsageLedger

router = APIRouter(prefix="/api")


@router.get("/health")
async def health(ctx: AppContext = Depends(context)) -> dict[str, Any]:
    snapshot = ctx.dispatcher.snapshot()
    loops = ctx.dispatcher.health.snapshot()
    holds = ctx.dispatcher.holds.active()
    return {
        "ok": ctx.dispatcher.health.ok if ctx.dispatcher.v3_enabled else True,
        "uptime_s": round((datetime.now().astimezone() - ctx.started_at).total_seconds(), 1),
        "version": "0.3.0",
        "build_id": build_id(),
        "loops": loops,
        "dispatch": {
            "engine": snapshot.get("engine"),
            "effective_state": snapshot.get("effective_state"),
            "sentence": snapshot.get("sentence"),
            "holds": [hold.id for hold in holds],
        },
        "starts": _read_starts(ctx.paths.run / "starts.json"),
    }


@router.get("/version")
async def version(ctx: AppContext = Depends(context)) -> dict[str, Any]:
    skill_config = await ctx.gateway.config()
    raw_claude = skill_config.get("claude")
    claude = cast(dict[str, Any], raw_claude) if isinstance(raw_claude, dict) else {}
    return {
        "app": "0.3.0",
        "git_sha": ctx.git_sha(),
        "build_id": build_id(),
        "skill_version": ctx.skill_version,
        "skill_schema": "solo-2611/1",
        "image": claude.get("image", ""),
        "harness_version": skill_config.get("harnessVersion", ""),
    }


@router.get("/system/status")
async def system_status(ctx: AppContext = Depends(context)) -> dict[str, Any]:
    return await ctx.system_status()


@router.post("/system/reconcile")
async def reconcile(ctx: AppContext = Depends(context)) -> dict[str, Any]:
    corrected = await ctx.reconciler.run()
    return {"corrected": corrected}


@router.post("/system/restart")
async def restart(payload: RestartRequest, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    recording = await ctx.recording.status()
    running_tasks = sorted(set(ctx.scheduler.active) | set(ctx.autopilot.agents))
    conflicts: list[dict[str, str]] = [
        {"kind": "container", "task_id": task_id} for task_id in running_tasks
    ]
    if recording.get("recording") or recording.get("pending"):
        conflicts.append({"kind": "recording", "task_id": str(recording.get("task_id") or "")})
    if conflicts and not payload.force:
        raise ApiError(
            409,
            "SYSTEM_BUSY",
            "仍有容器或录屏在运行；确认后可用强制重启，已有容器不会被中断",
            detail={"items": conflicts},
        )
    asyncio.get_running_loop().call_later(1.0, os.kill, os.getpid(), signal.SIGTERM)
    ctx.repo.add_event(
        "system.restart_requested",
        "已请求重启后端" + ("（强制）" if conflicts else ""),
        level="warn",
        actor="user",
        details={"running": conflicts},
    )
    return {
        "ok": True,
        "restarting": True,
        "build_id": build_id(),
        "hint": "若未安装 LaunchAgent，请回到终端重新运行 make app。",
    }


@router.post("/system/diagnostics")
async def diagnostics(ctx: AppContext = Depends(context)) -> dict[str, Any]:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = ctx.paths.backups / f"diagnostics-{stamp}.zip"
    info = {
        "version": "0.3.0",
        "build_id": build_id(),
        "git_sha": ctx.git_sha(),
        "skill_version": ctx.skill_version,
        "host": ctx.settings.host,
        "port": ctx.settings.port,
    }
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("version.json", json.dumps(info, ensure_ascii=False, indent=2))
        if ctx.paths.config.is_file():
            archive.write(ctx.paths.config, "config.toml")
        archive.writestr(
            "events.json",
            json.dumps(ctx.repo.events(limit=200), ensure_ascii=False, indent=2),
        )
        for path in ctx.paths.logs.rglob("*.log"):
            if path.is_file():
                archive.write(path, Path("logs") / path.relative_to(ctx.paths.logs))
    ctx.repo.add_event("system.diagnostics", f"诊断包已生成：{target}", actor="user")
    return {"path": str(target), "size": target.stat().st_size}


@router.get("/system/config")
async def get_config(ctx: AppContext = Depends(context)) -> dict[str, Any]:
    skill_config = await ctx.gateway.config()
    raw_skill_submission = skill_config.get("submission")
    skill_submission = cast(dict[str, Any], raw_skill_submission) if isinstance(raw_skill_submission, dict) else {}
    submission = ctx.settings.submission.model_dump()
    submission.update(
        {
            "auto_submit": bool(
                skill_submission.get("autoSubmit", submission["auto_submit"])
            ),
            "poll_interval_seconds": int(
                skill_submission.get(
                    "pollIntervalSeconds", submission["poll_interval_seconds"]
                )
            ),
            "poll_timeout_seconds": int(
                skill_submission.get(
                    "pollTimeoutSeconds", submission["poll_timeout_seconds"]
                )
            ),
        }
    )
    return {
        "host": ctx.settings.host,
        "port": ctx.settings.port,
        "skill_root": str(ctx.settings.skill_root),
        "skill_version": ctx.skill_version,
        "tasks_dir": str(ctx.settings.tasks_dir),
        "source": {
            **ctx.settings.source.model_dump(),
            "password": "<set>" if ctx.secrets.has("projecthub.password") else "<unset>",
            "bearer_token": "<set>" if ctx.secrets.has("projecthub.bearer") else "<unset>",
        },
        "execution": ctx.settings.execution.model_dump(),
        "storage": ctx.settings.storage.model_dump(),
        "recording": ctx.settings.recording.model_dump(),
        "submission": submission,
        "autopilot": {
            **ctx.settings.autopilot.model_dump(exclude={"breaker"}),
            "breaker": ctx.settings.autopilot.breaker.model_dump(),
        },
        "audit": ctx.settings.audit.model_dump(),
        "intake": ctx.settings.intake.model_dump(),
        "trash": ctx.settings.trash.model_dump(),
        "slim": ctx.settings.slim.model_dump(),
        "notify": {
            **ctx.settings.notify.model_dump(),
            "webhook_url": "<set>" if ctx.secrets.has("notify.webhook") else "",
        },
        "solo2": {
            **ctx.settings.solo2.model_dump(),
            "password": "<set>" if ctx.secrets.has("solo2.password") else "<unset>",
        },
        "skill_config": skill_config,
        "github_token": "<set>"
        if ctx.secrets.has("github.token") or _skill_has_github_token(skill_config)
        else "<unset>",
    }


@router.patch("/system/config")
async def patch_config(payload: ConfigPatchRequest, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    payload_data = payload.model_dump(exclude_none=True)
    autopilot_mode = (payload_data.get("autopilot") or {}).get("agent_mode") if isinstance(
        payload_data.get("autopilot"), dict
    ) else None
    if autopilot_mode and autopilot_mode != ctx.settings.autopilot.agent_mode:
        busy = [
            job.task_id
            for job in ctx.autopilot.jobs.values()
            if job.active and (job.desktop_running or job.task_id in ctx.autopilot.agents)
        ]
        if busy:
            raise ApiError(
                409,
                "AUTOPILOT_ACTIVE",
                "执行方式切换前必须先停止当前运行中的 Agent",
                detail={"task_ids": busy},
            )
    allowed_top = {"host", "port", "skill_root", "tasks_dir"}
    groups = {
        "source",
        "execution",
        "storage",
        "recording",
        "submission",
        "autopilot",
        "audit",
        "intake",
        "trash",
        "slim",
        "notify",
        "solo2",
        "secrets",
    }
    for key in payload_data:
        if key not in allowed_top | groups:
            raise ApiError(400, "CONFIG_KEY_INVALID", f"不支持的配置项：{key}")
    for key in allowed_top:
        if key in payload_data:
            setattr(ctx.settings, key, payload_data[key])
    nested = {
        "source": ctx.settings.source,
        "execution": ctx.settings.execution,
        "storage": ctx.settings.storage,
        "recording": ctx.settings.recording,
        "submission": ctx.settings.submission,
        "audit": ctx.settings.audit,
        "intake": ctx.settings.intake,
        "trash": ctx.settings.trash,
        "slim": ctx.settings.slim,
        "solo2": ctx.settings.solo2,
    }
    for group, model in nested.items():
        patch = payload_data.get(group)
        if not isinstance(patch, dict):
            continue
        allowed = {name: field for name, field in model.__class__.model_fields.items()}
        cleaned = {
            key: value for key, value in patch.items() if key in allowed and key not in {"password", "bearer_token"}
        }
        for key, value in cleaned.items():
            setattr(model, key, value)
    autopilot_patch = payload_data.get("autopilot")
    if isinstance(autopilot_patch, dict):
        breaker_patch = autopilot_patch.pop("breaker", None)
        for key, value in autopilot_patch.items():
            if key in ctx.settings.autopilot.model_fields:
                setattr(ctx.settings.autopilot, key, value)
        if isinstance(breaker_patch, dict):
            for key, value in breaker_patch.items():
                if key in ctx.settings.autopilot.breaker.model_fields:
                    setattr(ctx.settings.autopilot.breaker, key, value)
    notify_patch = payload_data.get("notify")
    if isinstance(notify_patch, dict):
        for key, value in notify_patch.items():
            if key == "webhook_url":
                continue
            if key in ctx.settings.notify.model_fields:
                setattr(ctx.settings.notify, key, value)
        if "webhook_url" in notify_patch:
            value = str(notify_patch["webhook_url"] or "")
            if value:
                ctx.secrets.set("notify.webhook", value)
                ctx.settings.notify.webhook_url = value
            else:
                ctx.secrets.delete("notify.webhook")
                ctx.settings.notify.webhook_url = ""
    secrets = payload_data.get("secrets")
    if isinstance(secrets, dict):
        for key, account in (
            ("source_password", "projecthub.password"),
            ("source_bearer", "projecthub.bearer"),
            ("solo2_password", "solo2.password"),
        ):
            if key in secrets:
                value = str(secrets[key] or "")
                if value:
                    ctx.secrets.set(account, value)
                else:
                    ctx.secrets.delete(account)
    ctx.save_settings()
    if "submission" in payload_data or "solo2" in payload_data:
        parsed = urlsplit(ctx.settings.solo2.base_url)
        server = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
        submission_pairs = [
            f"submission.autoSubmit={'true' if ctx.settings.submission.auto_submit else 'false'}",
            f"submission.pollIntervalSeconds={ctx.settings.submission.poll_interval_seconds}",
            f"submission.pollTimeoutSeconds={ctx.settings.submission.poll_timeout_seconds}",
            f"submission.server={server}",
        ]
        result = await ctx.gateway.call(["config", "set", *submission_pairs], timeout=60)
        if result.code != 0:
            raise ApiError(
                500,
                "SKILL_SUBMISSION_CONFIG_UPDATE_FAILED",
                result.err.strip() or "技能自动提交配置更新失败",
            )
    ctx.autopilot.apply_settings(ctx.settings.autopilot)
    ctx.audits.settings = ctx.settings.audit
    ctx.intake.intake_settings = ctx.settings.intake
    ctx.trash.settings = ctx.settings.trash
    ctx.janitor.settings = ctx.settings.slim
    ctx.notifier.settings = ctx.settings.notify
    ctx.reconciler.tasks_dir = ctx.settings.tasks_dir
    source_change = isinstance(payload_data.get("source"), dict)
    if source_change:
        await ctx.source.close()
        from solo_auto.sources.project_hub import ProjectHubClient

        ctx.source = ProjectHubClient(ctx.settings.source, ctx.secrets)
        ctx.source_online = False
        ctx.preflight.source = ctx.source
        ctx.intake.source = ctx.source
        ctx.ledger = UsageLedger(ctx.repo, ctx.settings.source.base_url)
        ctx.tasks.attach_ledger(ctx.ledger)
        ctx.autopilot.attach_ledger(ctx.ledger)
        ctx.batch.ledger = ctx.ledger
    execution = payload_data.get("execution")
    if isinstance(execution, dict):
        pairs: list[str] = []
        if "max_containers" in execution:
            pairs.append(f"claude.maxContainers={execution['max_containers']}")
        if "run_timeout_seconds" in execution:
            pairs.append(f"claude.timeoutSeconds={execution['run_timeout_seconds']}")
        if "attempts" in execution:
            pairs.append(f"claude.attempts={execution['attempts']}")
        if pairs:
            result = await ctx.gateway.call(["config", "set", *pairs], timeout=60)
            if result.code != 0:
                raise ApiError(500, "SKILL_CONFIG_UPDATE_FAILED", result.err.strip() or "技能配置更新失败")
            await ctx.scheduler.refresh_limit()
    ctx.repo.add_event("system.config_updated", "系统配置已更新", actor="user")
    return await get_config(ctx)


@router.post("/system/config/test")
async def test_config(payload: ConfigTestRequest, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    if payload.target == "skill":
        try:
            version_value = await ctx.gateway.version()
            return {"ok": True, "message": f"技能可用，版本 {version_value}", "detail": {}}
        except Exception as exc:
            return {"ok": False, "message": str(exc), "detail": {}}
    if payload.target == "source":
        return await ctx.source.test()
    if payload.target == "docker":
        ok = await ctx.docker_ok()
        return {
            "ok": ok,
            "message": "Docker 可用" if ok else "Docker daemon 不可用",
            "detail": {},
        }
    if payload.target == "solo2":
        return await ctx.solo2.test()
    if payload.target == "api":
        return await ctx.preflight.probe_api()
    raise ApiError(400, "TEST_TARGET_INVALID", "未知测试目标")


@router.post("/system/solo2/session")
async def solo2_session(request: Request, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    """Issue a short-lived SOLO2 session to the local skill only.

    Credentials remain owned by the workbench. The skill must not read the
    system keychain or reuse a cached browser session.
    """
    client_host = request.client.host if request.client else ""
    if client_host not in {"127.0.0.1", "::1", "localhost"}:
        raise ApiError(403, "LOCAL_ONLY", "SOLO2 会话只能由本机技能获取")
    result = await ctx.solo2.login_session()
    if not result.get("ok"):
        raise ApiError(
            502,
            "SOLO2_SESSION_FAILED",
            str(result.get("message") or "SOLO2 登录失败"),
            detail=result.get("detail") or {},
        )
    return result


@router.get("/system/logs")
async def logs(
    tail: int = Query(default=200, ge=1, le=1000),
    ctx: AppContext = Depends(context),
) -> dict[str, Any]:
    return {"items": ctx.repo.events(limit=tail)}


@router.post("/system/backup")
async def backup(ctx: AppContext = Depends(context)) -> dict[str, Any]:
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    target = ctx.paths.backups / f"backup-{stamp}.zip"
    with tempfile.TemporaryDirectory(prefix="solo-auto-backup-") as temporary:
        snapshot = Path(temporary) / "solo-auto.db"
        if ctx.paths.database.is_file():
            source_db = sqlite3.connect(ctx.paths.database)
            target_db = sqlite3.connect(snapshot)
            try:
                source_db.backup(target_db)
            finally:
                target_db.close()
                source_db.close()
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
            if snapshot.is_file():
                archive.write(snapshot, "solo-auto.db")
            if ctx.paths.config.is_file():
                archive.write(ctx.paths.config, "config.toml")
            for path in ctx.paths.drafts.rglob("*"):
                if path.is_file():
                    archive.write(path, Path("drafts") / path.relative_to(ctx.paths.drafts))
    ctx.repo.add_event("system.backup", f"备份已生成：{target}", actor="user")
    return {"path": str(target), "size": target.stat().st_size}


@router.post("/system/prune")
async def prune(payload: PruneRequest, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    candidates: list[Path] = []
    if "staging" in payload.scopes:
        candidates.extend(
            path for path in ctx.paths.staging.iterdir() if path.is_dir() and not (path / "meta.json").exists()
        )
    if "logs" in payload.scopes:
        cutoff = datetime.now().timestamp() - ctx.settings.storage.skill_log_retention_days * 86400
        candidates.extend(path for path in ctx.paths.skill_logs.rglob("*.log") if path.stat().st_mtime < cutoff)
    items = [{"path": str(path), "size": _path_size(path)} for path in candidates]
    if not payload.dry_run:
        if not payload.confirm:
            raise ApiError(422, "PRUNE_CONFIRM_REQUIRED", "实际清理前必须确认文件清单")
        for path in candidates:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
        ctx.repo.add_event("system.pruned", f"已清理 {len(candidates)} 个路径", level="warn", actor="user")
    return {"dry_run": payload.dry_run, "items": items, "total_bytes": sum(item["size"] for item in items)}


@router.post("/source/login")
async def source_login(payload: SourceLoginRequest, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    try:
        data = await ctx.source.login(payload.username or None, payload.password or None)
        ctx.settings.source.username = payload.username or ctx.settings.source.username
        ctx.save_settings()
        if payload.password:
            ctx.secrets.set("projecthub.password", payload.password)
        ctx.source_online = True
        return {
            "ok": True,
            "user": data.get("user") or {},
            "csrf_received": bool(data.get("csrf") or data.get("csrf_token")),
        }
    except SourceAuthError as exc:
        raise ApiError(401, "SOURCE_AUTH_FAILED", str(exc)) from exc
    except SourceOfflineError as exc:
        raise ApiError(503, "SOURCE_OFFLINE", str(exc)) from exc


@router.get("/source/projects")
async def projects(
    refresh: bool = False,
    ctx: AppContext = Depends(context),
) -> dict[str, Any]:
    try:
        items = await ctx.source.list_projects(refresh=refresh)
    except SourceAuthError as exc:
        raise ApiError(401, "SOURCE_AUTH_REQUIRED", str(exc)) from exc
    except SourceOfflineError as exc:
        raise ApiError(503, "SOURCE_OFFLINE", str(exc)) from exc
    ctx.source_online = True
    usage = {str(row["project_id"]): row for row in ctx.ledger.project_stats()}
    for item in items:
        row = usage.get(str(item.get("id") or ""), {})
        item["usage"] = {
            "used": int(row.get("used") or 0),
            "delivered": int(row.get("delivered") or 0),
            "submitted": int(row.get("submitted") or 0),
            "claimed_unused": int(row.get("claimed_unused") or 0),
            "delivery_rate": float(row.get("delivery_rate") or 0),
        }
    return {"items": items, "total": len(items), "page": 1, "page_size": len(items)}


@router.get("/source/projects/{project_id}/engineering")
async def engineering(
    project_id: str,
    q: str = "",
    language: str = "",
    min_files: int | None = None,
    max_files: int | None = None,
    tag_ids: list[str] = Query(default=[]),
    scope: str = "available",
    page: int = 1,
    page_size: int = 50,
    ctx: AppContext = Depends(context),
) -> dict[str, Any]:
    if not project_id.strip():
        raise ApiError(400, "PROJECT_REQUIRED", "请先选择项目")
    try:
        response = await ctx.source.list_engineering(
            project_id,
            q=q,
            language=language,
            min_files=min_files,
            max_files=max_files,
            tag_ids=tag_ids,
            scope=scope,
            page=page,
            page_size=page_size,
        )
        usage = {
            str(row["engineering_id"]): row
            for row in ctx.ledger.engineering_stats(project_id=project_id)
        }
        disabled = ctx.repo.disabled_engineering(project_id)
        mine: set[str] = set()
        if scope == "all":
            # 区分“自己的生产计划正在用”和“被别人占用”，题源页据此显示状态。
            held = await ctx.source.list_engineering(project_id, scope="mine", page_size=200)
            mine = {
                str(item.get("engineering_id") or item.get("id") or "")
                for item in held.get("items") or []
                if isinstance(item, dict)
            }
        for item in response.get("items") or []:
            if not isinstance(item, dict):
                continue
            engineering_id = str(item.get("engineering_id") or item.get("id") or "")
            off = disabled.get(engineering_id)
            item["disabled"] = off is not None
            item["disabled_reason"] = str(off.get("reason") or "") if off else ""
            if scope == "mine" or engineering_id in mine:
                item["holder"] = "mine"
            elif scope == "all" and (item.get("claimed_by") or item.get("claim_id")):
                item["holder"] = "other"
            else:
                item["holder"] = ""
            row = usage.get(engineering_id, {})
            item["usage"] = {
                "used": int(row.get("used") or 0),
                "delivered": int(row.get("delivered") or 0),
                "submitted": int(row.get("submitted") or 0),
                "claimed_unused": int(row.get("claimed_unused") or 0),
                "delivery_rate": float(row.get("delivery_rate") or 0),
                "last_outcome": str(row.get("last_outcome") or ""),
                "last_failure_kind": str(row.get("last_failure_kind") or ""),
            }
        return response
    except SourceAuthError as exc:
        raise ApiError(401, "SOURCE_AUTH_REQUIRED", str(exc)) from exc
    except SourceOfflineError as exc:
        raise ApiError(503, "SOURCE_OFFLINE", str(exc)) from exc


@router.get("/source/projects/{project_id}/disabled")
async def disabled_engineering(project_id: str, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    items = list(ctx.repo.disabled_engineering(project_id).values())
    return {"items": items, "total": len(items)}


@router.put("/source/projects/{project_id}/engineering/{engineering_id}/disabled")
async def set_engineering_disabled(
    project_id: str,
    engineering_id: str,
    payload: EngineeringDisableRequest,
    ctx: AppContext = Depends(context),
) -> dict[str, Any]:
    ctx.repo.set_engineering_disabled(
        project_id=project_id,
        engineering_id=engineering_id,
        disabled=payload.disabled,
        name=payload.name,
        reason=payload.reason,
    )
    label = payload.name or engineering_id
    ctx.repo.add_event(
        "source.engineering_disabled" if payload.disabled else "source.engineering_enabled",
        f"已禁用工程 {label}，生产计划不再领取" if payload.disabled else f"已重新启用工程 {label}",
        actor="user",
        details={"project_id": project_id, "engineering_id": engineering_id, "reason": payload.reason},
    )
    return {"ok": True, "engineering_id": engineering_id, "disabled": payload.disabled}


@router.get("/source/projects/{project_id}/engineering/{engineering_id}")
async def engineering_detail(
    project_id: str,
    engineering_id: str,
    ctx: AppContext = Depends(context),
) -> dict[str, Any]:
    return await ctx.source.detail(project_id, engineering_id)


@router.get("/source/assets/{asset_id}")
async def source_asset(asset_id: str, ctx: AppContext = Depends(context)) -> Response:
    try:
        content, media_type = await ctx.source.fetch_asset(asset_id)
    except SourceAuthError as exc:
        raise ApiError(401, "SOURCE_AUTH_REQUIRED", str(exc)) from exc
    except SourceOfflineError as exc:
        raise ApiError(503, "SOURCE_OFFLINE", str(exc)) from exc
    return Response(content=content, media_type=media_type, headers={"Cache-Control": "private, max-age=300"})


@router.post("/claims")
async def claim(payload: ClaimRequest, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    if not ctx.source_online:
        raise ApiError(503, "SOURCE_OFFLINE", "题源离线，禁止领取新工程")
    _require_disk(ctx)
    claimed: dict[str, Any] | None = None
    try:
        claimed = await ctx.source.claim(payload.project_id, payload.engineering_id)
        engineering_id = str(claimed.get("engineering_id") or payload.engineering_id)
        claim_id = str(claimed.get("claim_id") or engineering_id)
        projects = await ctx.source.list_projects()
        project = next((item for item in projects if str(item.get("id") or "") == payload.project_id), {})
        meta = await ctx.staging.stage(
            ctx.source,
            payload.project_id,
            engineering_id,
            claimed,
            claim_id,
            project_name=str(project.get("name") or payload.project_id),
        )
        ctx.ledger.record_claim(meta)
        ctx.repo.upsert_claim(
            {
                "id": claim_id,
                "project_id": payload.project_id,
                "engineering_id": engineering_id,
                "engineering_name": str(meta.get("name") or engineering_id),
                "claimed_at": str(meta.get("downloadedAt") or datetime.now().astimezone().isoformat()),
                "released_at": None,
                "task_id": None,
                "zip_sha256": meta.get("zipSha256"),
            }
        )
        ctx.repo.add_event(
            "claim.created",
            f"{meta.get('name')} 已领取并校验 ZIP",
            actor="user",
            details={"claim_id": claim_id, "sha256": meta.get("zipSha256")},
        )
        return {
            "claim_id": claim_id,
            "staging_id": engineering_id,
            "staging_path": meta.get("stagingPath"),
            "sha256_verified": bool(meta.get("zipSha256") == meta.get("expectedSha256")),
        }
    except SourceAuthError as exc:
        raise ApiError(401, "SOURCE_AUTH_REQUIRED", str(exc)) from exc
    except SourceOfflineError as exc:
        raise ApiError(503, "SOURCE_OFFLINE", str(exc)) from exc
    except Exception:
        if claimed:
            with suppress(Exception):
                await ctx.source.release(
                    payload.project_id,
                    payload.engineering_id,
                    str(claimed.get("claim_id") or payload.engineering_id),
                )
        raise


@router.delete("/claims/{claim_id}")
async def release_claim(
    claim_id: str,
    force: bool = False,
    ctx: AppContext = Depends(context),
) -> dict[str, Any]:
    meta = next((item for item in ctx.staging.list() if str(item.get("claimId")) == claim_id), None)
    if not meta:
        raise ApiError(404, "CLAIM_NOT_FOUND", f"领取记录不存在：{claim_id}")
    task_root = Path(str(meta.get("taskRoot") or ""))
    if task_root and meta.get("taskRoot"):
        task = ctx.repo.get_task_by_root(task_root)
        if task and task.get("job_status") not in {"exported", "archived", "aborted"} and not force:
            raise ApiError(409, "CLAIM_HAS_ACTIVE_TASK", "领取关联的任务尚未归档；确认放弃后才可归还")
        if task_root.is_dir():
            await reap_and_record(ctx.repo, task_root, task_id=task["id"] if task else None, actor="user")
            cleanup = await ctx.gateway.call(["cleanup", "--task-root", str(task_root)], timeout=120)
            if cleanup.code != 0:
                raise ApiError(
                    500,
                    "CLEANUP_FAILED",
                    cleanup.err.strip() or "容器清理失败，工程未归还",
                    detail={"stdout": cleanup.out, "stderr": cleanup.err},
                )
    try:
        await ctx.source.release(str(meta["projectId"]), str(meta["engineeringId"]), claim_id)
    except ApiError as exc:
        if exc.status_code != 404:
            raise
    ctx.repo.execute(
        "UPDATE claims SET released_at = ? WHERE id = ?",
        (datetime.now().astimezone().isoformat(), claim_id),
    )
    ctx.ledger.mark_released(str(meta["projectId"]), str(meta["engineeringId"]))
    ctx.staging.remove(str(meta["engineeringId"]))
    ctx.repo.add_event("claim.released", f"{meta.get('name')} 已归还", actor="user")
    return {"ok": True, "claim_id": claim_id}


@router.get("/claims")
async def claims(ctx: AppContext = Depends(context)) -> dict[str, Any]:
    items = [item for item in ctx.staging.list() if not item.get("taskRoot")]
    return {"items": items, "total": len(items), "page": 1, "page_size": len(items)}


@router.get("/staging")
async def staging(ctx: AppContext = Depends(context)) -> dict[str, Any]:
    items = [
        {
            **item,
            "size": _path_size(Path(str(item.get("stagingPath") or ""))),
        }
        for item in ctx.staging.list()
    ]
    return {"items": items, "total": len(items)}


@router.post("/tasks", status_code=201)
async def create_task(payload: CreateTaskRequest, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    _require_disk(ctx)
    return await ctx.tasks.create(
        staging_id=payload.staging_id,
        task_name=payload.task_name,
        domain=payload.domain,
        sub_direction=payload.sub_direction,
    )


def _read_starts(path: Path) -> list[dict[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _skill_has_github_token(config: dict[str, Any]) -> bool:
    github = config.get("github")
    return bool(isinstance(github, dict) and github.get("accessTokens"))


def _path_size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    if not path.is_dir():
        return 0
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def _require_disk(ctx: AppContext) -> None:
    free_gb = shutil.disk_usage(ctx.paths.home).free / 1024**3
    if free_gb < ctx.settings.storage.disk_floor_gb:
        raise ApiError(
            503,
            "DISK_LOW",
            f"磁盘剩余 {free_gb:.1f}GB，低于阈值 {ctx.settings.storage.disk_floor_gb:g}GB，禁止新开工",
        )
