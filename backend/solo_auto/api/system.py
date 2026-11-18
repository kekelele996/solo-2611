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
from typing import Any, Literal, cast
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidTag
from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, Field

from solo_auto import account_profile, skill_profile
from solo_auto.account_binding import origin, submission_quota_directory
from solo_auto.api.account_view import require_access, trusted_skill
from solo_auto.api.dependencies import context
from solo_auto.api.profiles import serialize_account_changes, standby_credentials
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
from solo_auto.sources import github
from solo_auto.sources.solo2 import Solo2Client

router = APIRouter(prefix="/api")


@router.get("/health")
async def health(ctx: AppContext = Depends(context)) -> dict[str, Any]:
    snapshot = ctx.dispatcher.status_summary()
    loops = ctx.dispatcher.health.snapshot()
    holds = ctx.dispatcher.holds.active()
    return {
        "ok": ctx.dispatcher.health.ok if ctx.dispatcher.v3_enabled else True,
        "uptime_s": round((datetime.now().astimezone() - ctx.started_at).total_seconds(), 1),
        "version": "0.4.0",
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
    skill_config = _runtime_profile(ctx)
    raw_claude = skill_config.get("claude")
    claude = cast(dict[str, Any], raw_claude) if isinstance(raw_claude, dict) else {}
    return {
        "app": "0.4.0",
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
        "version": "0.4.0",
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


def _runtime_profile(ctx: AppContext) -> dict[str, Any]:
    limit = ctx.dispatcher.effective_container_limit if ctx.dispatcher.v3_enabled else ctx.settings.execution.max_containers
    profile = skill_profile.runtime_profile(ctx.settings, limit)
    profile["submission"]["requireAccountClaim"] = bool(ctx.routing.enabled or (ctx.settings.solo2.username and ctx.settings.submission.auto_submit))
    return profile


@router.get("/system/skill/config")
async def skill_runtime(request: Request, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    if not trusted_skill(request):
        require_access(request, ctx)
    return _runtime_profile(ctx)


@router.get("/system/skill/task-account", include_in_schema=False)
async def skill_task_account(request: Request, task_root: str = "", claim_key: str = "", start: bool = False,
                             source: str = "", tasks_dir: str = "",
                             ctx: AppContext = Depends(context)) -> dict[str, Any]:
    if not trusted_skill(request):
        raise ApiError(403, "LOCAL_ONLY", "任务归属仅向本机技能开放")
    root = Path(task_root).resolve() if task_root else None
    if root and root.parent != ctx.settings.tasks_dir.resolve():
        raise ApiError(409, "TASK_SOURCE_REVIEW", "工作目录不在该工作台任务目录中")
    route = ctx.routing.account_config(task_root=root, claim_key=claim_key)
    if claim_key:
        row = ctx.routing.row(claim_key=claim_key)
        if (not row or row["claim_state"] != "held" or row["task_id"] or not source
                or source != row["meta"].get("initSourcePath") or tasks_dir != str(ctx.settings.tasks_dir.resolve())
                or ctx.routing.drop_reason(row)):
            raise ApiError(409, "TASK_SOURCE_REVIEW", "领取凭证与本次初始化的源码目录不一致，或已退出调度")
    if root and route:
        ctx.routing.audit(ctx.repo.task_id_for_root(root), root, starting=start)
    return {"route": route}


class SubmissionSignal(BaseModel):
    account: str
    server: str
    role: Literal["primary", "standby"]
    observed_at: datetime | None = None


@router.post("/system/skill/submission-capacity", include_in_schema=False)
async def skill_submission_capacity(payload: SubmissionSignal, request: Request, force: bool = False,
                                     ctx: AppContext = Depends(context)) -> dict[str, Any]:
    if not trusted_skill(request):
        raise ApiError(403, "LOCAL_ONLY", "提交额度仅向本机技能开放")
    if (payload.account, origin(payload.server)) != (ctx.routing.account(payload.role)[0], origin(ctx.settings.solo2.base_url)):
        raise ApiError(409, "TASK_OWNER_CHANGED", "提交额度查询的账号不匹配")
    await ctx.routing.refresh(payload.role, force=force)
    result = ctx.routing.quota(payload.role)
    if result["account"] != payload.account:
        raise ApiError(409, "TASK_OWNER_CHANGED", "查询期间提交账号已切换")
    return {key: result[key] for key in ("open", "fresh", "submitted", "limit", "error")}


@router.post("/system/skill/submission-deferred", include_in_schema=False)
async def skill_submission_deferred(payload: SubmissionSignal, request: Request,
                                     ctx: AppContext = Depends(context)) -> dict[str, bool]:
    if not trusted_skill(request):
        raise ApiError(403, "LOCAL_ONLY", "额度回执仅接受本机技能通知")
    if not payload.observed_at or payload.observed_at.tzinfo is None:
        raise ApiError(422, "QUOTA_TIMESTAMP", "额度回执缺少北京时间")
    ctx.routing.notify_deferred(payload.role, payload.observed_at, account=payload.account, server=payload.server)
    return {"ok": True}


@router.post("/system/skill/credential/{purpose}", include_in_schema=False)
async def skill_credential(purpose: Literal["github", "github_standby", "model"], request: Request, response: Response,
                           ctx: AppContext = Depends(context)) -> dict[str, str]:
    if (not request.client or request.client.host not in {"127.0.0.1", "::1"}
            or request.url.hostname not in {"127.0.0.1", "::1", "localhost"}
            or "origin" in request.headers or request.headers.get("x-solo-skill") != "solo-2611"):
        raise ApiError(403, "LOCAL_ONLY", "凭据只能由本机技能领取")
    value: str | None
    if purpose == "github_standby":
        async with ctx.profile_lock:
            standby = standby_credentials(ctx)
            if (not standby or request.headers.get("x-account-profile") != ctx.settings.active_profile
                    or request.headers.get("x-standby-revision") != standby.revision):
                raise ApiError(409, "STANDBY_CHANGED", "副号绑定已变更，请重新预检")
            value = standby.github_token
    else:
        value = ctx.secrets.get("github.token" if purpose == "github" else "model.key")
    if not value:
        raise ApiError(409, "CREDENTIAL_MISSING", "请在工作台保存对应凭据")
    response.headers["Cache-Control"] = "no-store"
    return {"value": value}


@router.get("/system/config")
async def get_config(request: Request, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    locked = not ctx.account_access.allows(request.headers.get("x-account-view", ""))
    if trusted_skill(request):
        standby = standby_credentials(ctx)
        return {
            "active_profile": ctx.settings.active_profile,
            "submission": {"auto_submit": ctx.settings.submission.auto_submit,
                           "daily_new_limit": ctx.settings.submission.daily_new_limit,
                           "quota_directory": str(ctx.paths.run / "solo2-daily-quota")},
            "solo2": {
                "username": ctx.settings.solo2.username,
                "base_url": ctx.settings.solo2.base_url,
                "locked_domain": ctx.settings.solo2.locked_domain,
                "password": "<set>" if ctx.secrets.has("solo2.password") else "<unset>",
                "quota_directory": submission_quota_directory(ctx, ctx.settings.solo2.username),
                "standby": {
                    "username": standby.username,
                    "base_url": ctx.settings.solo2.base_url,
                    "locked_domain": standby.domain,
                    "quota_directory": submission_quota_directory(ctx, standby.username),
                    "revision": standby.revision,
                } if standby else None,
            },
        }
    skill_config = {} if locked else _runtime_profile(ctx)
    execution = ctx.settings.execution.model_dump()
    submission = ctx.settings.submission.model_dump()
    submission["poll_timeout_seconds"] = min(600, submission["poll_timeout_seconds"])
    submission["poll_interval_seconds"] = min(300, submission["poll_interval_seconds"])
    return {
        "accounts_locked": locked,
        "active_profile": None if locked else ctx.settings.active_profile,
        "host": ctx.settings.host,
        "port": ctx.settings.port,
        "skill_root": str(ctx.settings.skill_root),
        "skill_version": ctx.skill_version,
        "tasks_dir": str(ctx.settings.tasks_dir),
        "source": {} if locked else {
            **ctx.settings.source.model_dump(),
            "password": "<set>" if ctx.secrets.has("projecthub.password") else "<unset>",
            "bearer_token": "<set>" if ctx.secrets.has("projecthub.bearer") else "<unset>",
        },
        "execution": execution,
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
        "solo2": {} if locked else {
            **ctx.settings.solo2.model_dump(),
            "password": "<set>" if ctx.secrets.has("solo2.password") else "<unset>",
        },
        "skill_config": skill_config,
        "github_token": "<set>" if not locked and ctx.secrets.has("github.token") else "<unset>",
        "gateway_key": "<set>" if not locked and ctx.secrets.has("model.key") else "<unset>",
        "model_base_url": "" if locked else ctx.settings.skill.base_url,
    }


@router.patch("/system/config", dependencies=[Depends(serialize_account_changes)])
async def patch_config(payload: ConfigPatchRequest, request: Request, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    payload_data = payload.model_dump(exclude_none=True)
    if payload_data.keys() & {"solo2", "source", "skill", "secrets"}:
        require_access(request, ctx)
        expected = request.headers.get("x-account-profile")
        if expected and expected != ctx.settings.active_profile:
            raise ApiError(409, "PROFILE_CHANGED", "当前配置已切换，请重新打开账号设置")
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
    account_patch = payload_data.get("solo2") or {}
    identity_changed = any(key in account_patch and account_patch[key] != getattr(ctx.settings.solo2, key)
                           for key in ("base_url", "username"))
    source_patch = payload_data.get("source") or {}
    owner_changed = identity_changed or any(
        key in source_patch and source_patch[key] != getattr(ctx.settings.source, key) for key in ("base_url", "username")
    ) or ("locked_domain" in account_patch and account_patch["locked_domain"] != ctx.settings.solo2.locked_domain)
    if owner_changed and ctx.routing.busy():
        raise ApiError(409, "ROUTED_TASKS_ACTIVE", "请先处理当前账号的在途任务及待归还工程")
    if identity_changed:
        payload_data.setdefault("secrets", {}).setdefault("solo2_password", "")
        account_patch.setdefault("locked_domain", "")
    allowed_top = {"host", "port", "skill_root", "tasks_dir"}
    groups = {
        "skill",
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
    account_patch = payload_data.get("solo2") or {}
    if "locked_domain" in account_patch:
        ctx.settings.solo2.managed_domain = True
    if "github_token" in (payload_data.get("secrets") or {}):
        ctx.settings.solo2.managed_github = True
    nested = {
        "skill": ctx.settings.skill,
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
            ("gateway_key", "model.key"),
            ("github_token", "github.token"),
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
    ctx.autopilot.apply_settings(ctx.settings.autopilot)
    ctx.audits.settings = ctx.settings.audit
    ctx.intake.intake_settings = ctx.settings.intake
    ctx.trash.settings = ctx.settings.trash
    ctx.janitor.settings = ctx.settings.slim
    ctx.notifier.settings = ctx.settings.notify
    ctx.reconciler.tasks_dir = ctx.settings.tasks_dir
    source_change = isinstance(payload_data.get("source"), dict)
    if source_change or "source_password" in (payload_data.get("secrets") or {}):
        from solo_auto.sources.project_hub import ProjectHubClient

        previous = ctx.source
        ctx.replace_source(ProjectHubClient(ctx.settings.source, ctx.secrets))
        await previous.close()
    if owner_changed or "github_token" in (payload_data.get("secrets") or {}):
        await ctx.routing.invalidate()
    execution = payload_data.get("execution")
    if isinstance(execution, dict):
        await ctx.scheduler.refresh_limit()
        ctx.settings.execution.attempts = 1
        ctx.scheduler.attempt_total = 1
        ctx.scheduler.run_mode = ctx.settings.execution.run_mode
    ctx.repo.add_event("system.config_updated", "系统配置已更新", actor="user")
    return await get_config(request, ctx)


@router.post("/system/accounts/export", dependencies=[Depends(require_access), Depends(serialize_account_changes)])
async def export_accounts(payload: dict[str, str], ctx: AppContext = Depends(context)) -> dict[str, Any]:
    password = payload.get("password", "")
    profile: dict[str, Any] = {"solo2": ctx.settings.solo2.model_dump(exclude={"managed_domain", "managed_github"})}
    if password:
        profile["secrets"] = {k: ctx.secrets.get(v) or "" for k, v in
                              (("solo2_password", "solo2.password"), ("github_token", "github.token"))}
    return await asyncio.to_thread(account_profile.pack, profile, password)


@router.post("/system/accounts/import", dependencies=[Depends(require_access), Depends(serialize_account_changes)])
async def import_accounts(payload: dict[str, Any], request: Request, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    try:
        profile = await asyncio.to_thread(account_profile.unpack, payload["bundle"], payload.get("password", ""))
        account = type(ctx.settings.solo2).model_validate(profile["solo2"])
        if urlsplit(account.base_url).scheme not in {"https", "http"} or not urlsplit(account.base_url).hostname:
            raise ValueError("无效平台地址")
        secrets = profile.get("secrets", {})
        if not isinstance(secrets, dict) or set(secrets) - {"github_token", "solo2_password"}:
            raise ValueError("无效凭据字段")
        if (account.username, account.base_url) != (ctx.settings.solo2.username, ctx.settings.solo2.base_url):
            secrets.setdefault("solo2_password", "")
        patch = ConfigPatchRequest.model_validate({"solo2": account.model_dump(), "secrets": secrets})
    except (KeyError, TypeError, ValueError, InvalidTag):
        raise ApiError(422, "ACCOUNT_IMPORT_INVALID", "配置文件或备份密码不正确") from None
    return await patch_config(patch, request, ctx)


@router.post("/system/accounts/migrate", dependencies=[Depends(require_access), Depends(serialize_account_changes)])
async def migrate_account(request: Request, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    codex = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()
    path = Path(os.environ.get("SOLO2611_CONFIG") or codex / "solo-2611/config.json").expanduser()
    if not path.exists() and path.parent.is_dir():
        # 仅在用户点击迁入时选用安装程序移出的最近一份备份。
        prefix = path.name + ".retired-"
        path = max((entry for entry in path.parent.iterdir() if entry.name.startswith(prefix) and entry.is_file()),
                   default=path)
    if not path.is_file():
        raise ApiError(404, "PROFILE_MISSING", "没有找到技能账号配置")
    try:
        previous = json.loads(path.read_text())
        token = previous.get("github", {}).get("accessTokens")
        if isinstance(token, list):
            token = next(iter(token), "")
        domain = ctx.settings.solo2.locked_domain if ctx.settings.solo2.managed_domain else previous.get("submission", {}).get("lockedDomain", "")
        current_token = ctx.secrets.get("github.token")
        effective_token = current_token if ctx.settings.solo2.managed_github else current_token or token
        secrets = {"github_token": effective_token} if effective_token else {}
        runtime = None
        if not ctx.settings.skill.migrated:
            runtime = skill_profile.legacy_runtime(previous)
            if ctx.settings.skill.base_url != type(runtime)().base_url or ctx.secrets.has("model.key"):
                runtime.base_url = ctx.settings.skill.base_url
            gateway_key = previous.get("claude", {}).get("apiKey")
            if gateway_key and not ctx.secrets.has("model.key"):
                secrets["gateway_key"] = gateway_key
            runtime.migrated = True
        patch = ConfigPatchRequest.model_validate({"solo2": {"locked_domain": domain}, "secrets": secrets})
    except (OSError, AttributeError, TypeError, ValueError):
        raise ApiError(422, "PROFILE_INVALID", "原账号配置无法读取，请检查配置格式") from None
    if runtime is not None:
        ctx.settings.skill = runtime
    return await patch_config(patch, request, ctx)


@router.post("/system/config/test")
async def test_config(payload: ConfigTestRequest, request: Request, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    if payload.target in {"solo2", "github", "source"}:
        require_access(request, ctx)
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
    if payload.target == "github":
        return await github.test_credentials(ctx.secrets.get("github.token"))
    if payload.target == "solo2":
        return await ctx.solo2.test()
    if payload.target == "api":
        return await ctx.preflight.probe_api()
    raise ApiError(400, "TEST_TARGET_INVALID", "未知测试目标")


@router.post("/system/solo2/session", dependencies=[Depends(serialize_account_changes)])
async def solo2_session(request: Request, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    """Issue a short-lived SOLO2 session to the local skill only.

    Credentials remain owned by the workbench. The skill must not read the
    system keychain or reuse a cached browser session.
    """
    client_host = request.client.host if request.client else ""
    if client_host not in {"127.0.0.1", "::1", "localhost"}:
        raise ApiError(403, "LOCAL_ONLY", "SOLO2 会话只能由本机技能获取")
    if not trusted_skill(request) and ctx.account_access.configured:
        raise ApiError(403, "LOCAL_ONLY", "SOLO2 会话只能由本机技能获取")
    expected = request.headers.get("x-account-profile")
    if expected and expected != ctx.settings.active_profile:
        raise ApiError(409, "PROFILE_CHANGED", "当前用户配置已切换，请重新预检")
    payload = await request.json() if await request.body() else {}
    if (payload.get("expected_account", ctx.settings.solo2.username) != ctx.settings.solo2.username
            or payload.get("expected_server", origin(ctx.settings.solo2.base_url)) != origin(ctx.settings.solo2.base_url)):
        raise ApiError(409, "PROFILE_CHANGED", "当前主号已切换，请重新预检")
    result = await ctx.solo2.login_session()
    if not result.get("ok"):
        raise ApiError(
            502,
            "SOLO2_SESSION_FAILED",
            str(result.get("message") or "SOLO2 登录失败"),
            detail=result.get("detail") or {},
        )
    return {**result, "active_profile": ctx.settings.active_profile, "locked_domain": ctx.settings.solo2.locked_domain,
            "auto_submit": ctx.settings.submission.auto_submit}


class StandbySessionRequest(BaseModel):
    primary_username: str = Field(min_length=1, max_length=200, pattern=r'\S')
    primary_address: str = Field(min_length=1, max_length=2000)


@router.post("/system/solo2/standby-session", dependencies=[Depends(serialize_account_changes)])
async def solo2_standby_session(payload: StandbySessionRequest, request: Request,
                                ctx: AppContext = Depends(context)) -> dict[str, Any]:
    if (not request.client or request.client.host not in {"127.0.0.1", "::1"}
            or request.url.hostname not in {"127.0.0.1", "::1", "localhost"}
            or "origin" in request.headers or request.headers.get("x-solo-skill") != "solo-2611"):
        raise ApiError(403, "LOCAL_ONLY", "副号会话只能由本机技能获取")
    expected = request.headers.get("x-account-profile")
    if expected and expected != ctx.settings.active_profile:
        raise ApiError(409, "PROFILE_CHANGED", "当前用户配置已切换，请重新预检")
    standby = standby_credentials(ctx)
    primary = ctx.settings.solo2
    if (not standby or standby.primary_username != payload.primary_username.strip()
            or standby.primary_address != payload.primary_address.strip()):
        raise ApiError(409, "STANDBY_CHANGED", "副号绑定已变更，请重新预检")
    settings = primary.model_copy(update={"username": standby.username, "locked_domain": standby.domain})
    result = await Solo2Client(settings, ctx.secrets).login_session(password=standby.password)
    if not result.get("ok"):
        raise ApiError(502, "SOLO2_STANDBY_SESSION_FAILED",
                       str(result.get("message") or "副号 SOLO2 登录失败"), detail=result.get("detail") or {})
    return {**result, "active_profile": ctx.settings.active_profile, "locked_domain": standby.domain,
            "standby": True, "standby_revision": standby.revision, "auto_submit": ctx.settings.submission.auto_submit}


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
            path for path in ctx.staging.root.glob("*") if path.name != ".profiles" and path.is_dir() and not (path / "meta.json").exists()
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


@router.post("/source/login", dependencies=[Depends(serialize_account_changes)])
async def source_login(payload: SourceLoginRequest, request: Request, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    if not payload.username or not payload.password:
        require_access(request, ctx)
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


@router.post("/claims", dependencies=[Depends(serialize_account_changes)])
async def claim(payload: ClaimRequest, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    if not ctx.source_online:
        raise ApiError(503, "SOURCE_OFFLINE", "题源离线，禁止领取新工程")
    _require_disk(ctx)
    if ctx.routing.enabled:
        async with ctx.intake._lock:
            if payload.project_id != ctx.routing.primary_project()[0]:
                raise ApiError(409, "POOL_PROJECT_FORBIDDEN", "请从题源页当前的主号项目池领取工程")
            await ctx.routing.verify_primary_github()
            lane = ctx.routing.lane("primary")
            if not lane or lane.route["projectId"] != payload.project_id:
                raise ApiError(409, "POOL_PROJECT_FORBIDDEN", "请从已核验的主号专属工程池领取工程")
            await ctx.routing.refresh("primary")
            role, reason = ctx.routing.selection()
            if role != "primary":
                raise ApiError(409, "PRIMARY_QUOTA_UNAVAILABLE", reason)
            try:
                await lane.source.verify_identity()
                meta = await ctx.routing.claim_engineering(lane, payload.engineering_id)
            except (SourceAuthError, SourceOfflineError) as exc:
                raise ApiError(409, "POOL_SOURCE_UNAVAILABLE", str(exc)) from exc
            return {"claim_id": meta["claimKey"], "staging_id": meta["stagingId"], "staging_path": meta["stagingPath"],
                    "sha256_verified": bool(meta["zipSha256"] == meta["expectedSha256"])}
    if ctx.settings.solo2.username and ctx.settings.submission.auto_submit:
        raise ApiError(409, "POOL_REQUIRED", "请先下达主号生产计划并核对题源项目")
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


@router.delete("/claims/{claim_id}", dependencies=[Depends(serialize_account_changes)])
async def release_claim(
    claim_id: str,
    force: bool = False,
    ctx: AppContext = Depends(context),
) -> dict[str, Any]:
    if row := ctx.routing.row(claim_key=claim_id):
        async with ctx.intake._lock:
            if row["task_id"]:
                raise ApiError(409, "CLAIM_HAS_ACTIVE_TASK", "请在对应任务详情归档并归还工程")
            await ctx.routing.release(row["meta"])
            return {"ok": True, "claim_id": claim_id}
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
    lane = ctx.routing.lane("primary")
    staged = ctx.staging.list() + (lane.staging.list() if lane else [])
    items = [{**item, "claimId": item.get("claimKey") or item["claimId"]} for item in staged if not item.get("taskRoot")]
    return {"items": items, "total": len(items), "page": 1, "page_size": len(items)}


@router.get("/staging")
async def staging(ctx: AppContext = Depends(context)) -> dict[str, Any]:
    lane = ctx.routing.lane("primary")
    staged = ctx.staging.list() + (lane.staging.list() if lane else [])
    items = [
        {
            **item,
            "claimId": item.get("claimKey") or item["claimId"],
            "size": _path_size(Path(str(item.get("stagingPath") or ""))),
        }
        for item in staged
    ]
    return {"items": items, "total": len(items)}


@router.post("/tasks", status_code=201, dependencies=[Depends(serialize_account_changes)])
async def create_task(payload: CreateTaskRequest, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    _require_disk(ctx)
    async with ctx.intake._lock:
        lane = ctx.routing.lane("primary") if ctx.routing.row(claim_key=payload.staging_id) else None
        return await ctx.tasks.create(
            staging_id=payload.staging_id, task_name=payload.task_name, domain=payload.domain, sub_direction=payload.sub_direction,
            staging=lane.staging if lane else None, ledger=lane.ledger if lane else None,
        )


def _read_starts(path: Path) -> list[dict[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


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
