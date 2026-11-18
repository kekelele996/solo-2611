"""Named account configurations. Only the active profile is used by workbench clients."""
from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field, field_validator

from solo_auto.api.account_view import (
    Connection,
    PasswordTarget,
    connections,
    display_address,
    local_page,
    require_access,
)
from solo_auto.api.dependencies import context
from solo_auto.api.types import SecretConfigPatch
from solo_auto.context import AppContext
from solo_auto.errors import ApiError
from solo_auto.secrets import SecretStore
from solo_auto.settings import AccountProfile, Solo2Settings, SourceSettings, save_settings
from solo_auto.sources import github
from solo_auto.sources.project_hub import ProjectHubClient
from solo_auto.sources.solo2 import Solo2Client


def profile_access(request: Request, ctx: AppContext = Depends(context)) -> None:
    if not ctx.account_access.unlocked(request.headers.get('x-account-view', '')):
        raise ApiError(403, 'ACCOUNT_LOCKED', '请先设置并输入查看码')


async def serialize_account_changes(ctx: AppContext = Depends(context)) -> AsyncIterator[None]:
    async with ctx.profile_lock:
        yield


router = APIRouter(prefix='/api/system/accounts/profiles', dependencies=[Depends(local_page), Depends(profile_access)])


class ProfileItem(BaseModel):
    id: str
    name: str


class StandbyAccount(BaseModel):
    username: str = Field(min_length=1, max_length=200, pattern=r'\S')
    domain: Literal['数据工程', '安全', '游戏、图形与多媒体']
    revision: str = ''


class StandbyUpdate(StandbyAccount):
    primary_username: str
    primary_address: str
    password: str = ''
    github_token: str = ''


class ConnectionCheck(BaseModel):
    ok: bool
    message: str


class StandbyCheck(BaseModel):
    solo2: ConnectionCheck
    github: ConnectionCheck


class Profiles(BaseModel):
    active: str
    items: list[ProfileItem]
    connections: list[Connection]
    standby: StandbyAccount | None = None


class NewProfile(AccountProfile):
    name: str = Field(min_length=1, max_length=60, pattern=r'\S')
    secrets: SecretConfigPatch = Field(default_factory=SecretConfigPatch)

    @field_validator('name')
    @classmethod
    def clean_name(cls, value: str) -> str:
        return value.strip()


@router.get('')
async def list_profiles(ctx: AppContext = Depends(context)) -> Profiles:
    return profile_catalog(ctx)


def profile_catalog(ctx: AppContext) -> Profiles:
    settings = ctx.settings
    record = ctx.secrets.get_json(_standby_key(settings.solo2))
    return Profiles(active=settings.active_profile, items=[ProfileItem(id=k, name=v.name) for k, v in settings.profiles.items()],
                    connections=connections(ctx), standby=StandbyAccount.model_validate(record) if record else None)


def _standby_key(primary: Solo2Settings) -> str:
    # SecretStore 负责按配置 ID 隔离。同一配置内更换主号时也只取相应主号的绑定。
    origin = urlsplit(primary.base_url)
    identity = f'{origin.scheme}://{origin.netloc}/{primary.username.strip()}'
    return 'solo2.standby.' + hashlib.sha256(identity.encode()).hexdigest()


def submission_quota_directory(ctx: AppContext, username: str) -> str:
    url = urlsplit(ctx.settings.solo2.base_url.strip())
    identity = f'{url.scheme}://{url.netloc}/{username.strip()}'
    return str(ctx.paths.run / 'solo2-daily-quota' / hashlib.sha256(identity.encode()).hexdigest())


def standby_record(ctx: AppContext) -> dict[str, Any] | None:
    primary = ctx.settings.solo2
    record = ctx.secrets.get_json(_standby_key(primary))
    return record if record and primary.username.strip() else None


def standby_credentials(ctx: AppContext) -> StandbyUpdate | None:
    primary = ctx.settings.solo2
    record = standby_record(ctx)
    if not record:
        return None
    standby = StandbyUpdate.model_validate({**record, 'primary_username': primary.username.strip(),
                                           'primary_address': display_address(primary.base_url)})
    if standby.username == standby.primary_username or not standby.password or not standby.github_token:
        return None
    return standby


def _standby_draft(profile_id: str, payload: StandbyUpdate, request: Request, ctx: AppContext) -> StandbyUpdate:
    if not ctx.account_access.unlocked(request.headers.get('x-account-view', ''), settings=True):
        raise ApiError(403, 'ACCOUNT_LOCKED', '请先解锁设置页')
    primary = ctx.settings.solo2
    if (profile_id != ctx.settings.active_profile or primary.username != payload.primary_username
            or display_address(primary.base_url) != payload.primary_address):
        raise ApiError(409, 'PROFILE_CHANGED', '当前主号已变更，请重新打开副号设置')
    username = payload.username.strip()
    if not primary.username.strip() or primary.username.strip() == username:
        raise ApiError(422, 'STANDBY_IDENTITY', '请先配置主号，并填写不同的副号账号')
    previous: dict[str, Any] = ctx.secrets.get_json(_standby_key(primary)) or {}
    if previous.get('revision', '') != payload.revision:
        raise ApiError(409, 'STANDBY_CHANGED', '副号配置已更新，请重新打开副号设置')
    same_account = previous.get('username') == username
    return payload.model_copy(update={
        'username': username,
        'password': payload.password or (previous.get('password', '') if same_account else ''),
        'github_token': payload.github_token.strip() or (previous.get('github_token', '') if same_account else ''),
    })


@router.put('/{profile_id}/standby', dependencies=[Depends(serialize_account_changes)])
async def save_standby(profile_id: str, payload: StandbyUpdate, request: Request,
                       ctx: AppContext = Depends(context)) -> Profiles:
    draft = _standby_draft(profile_id, payload, request, ctx)
    if not draft.password or not draft.github_token:
        raise ApiError(422, 'STANDBY_CREDENTIALS', '请填写该副号的 SOLO2 密码和专属 GitHub 密钥')
    key = _standby_key(ctx.settings.solo2)
    record = draft.model_dump(exclude={'primary_username', 'primary_address'})
    record['revision'] = uuid4().hex
    try:
        ctx.secrets.set_json(key, record)
        saved = ctx.secrets.get_json(key)
    except OSError:
        raise ApiError(500, 'STANDBY_SAVE_FAILED', '无法写入或读取本机副号凭据，请重试') from None
    if saved != record:
        raise ApiError(500, 'STANDBY_SAVE_FAILED', '副号配置保存后无法核对，请重试')
    return profile_catalog(ctx)


@router.post('/{profile_id}/standby/test')
async def test_standby(profile_id: str, payload: StandbyUpdate, request: Request,
                       ctx: AppContext = Depends(context)) -> StandbyCheck:
    async with ctx.profile_lock:
        draft = _standby_draft(profile_id, payload, request, ctx)
        settings = ctx.settings.solo2.model_copy(update={'username': draft.username, 'locked_domain': draft.domain})
        client = Solo2Client(settings, ctx.secrets)
    solo2_result, github_result = await asyncio.gather(
        client.test(password=draft.password), github.test_credentials(draft.github_token),
    )
    return StandbyCheck(solo2=ConnectionCheck.model_validate(solo2_result),
                        github=ConnectionCheck.model_validate(github_result))


@router.post('', status_code=201, dependencies=[Depends(serialize_account_changes)])
async def create_profile(payload: NewProfile, request: Request, ctx: AppContext = Depends(context)) -> Profiles:
    if not ctx.account_access.unlocked(request.headers.get('x-account-view', ''), settings=True):
        raise ApiError(403, 'ACCOUNT_LOCKED', '请解锁设置页以创建配置')
    if any(item.name.casefold() == payload.name.casefold() for item in ctx.settings.profiles.values()):
        raise ApiError(409, 'PROFILE_EXISTS', '配置名称已使用')
    for url in (payload.solo2.base_url, payload.source.base_url, payload.model_base_url):
        try:
            target = urlsplit(url)
            if not target.hostname or target.scheme not in {'https', 'http'} or target.username or target.password:
                raise ValueError
            _ = target.port
        except ValueError:
            raise ApiError(422, 'PROFILE_URL', '请填写完整的 HTTP 或 HTTPS 连接地址') from None
    key = uuid4().hex
    profile = AccountProfile.model_validate(payload.model_dump(exclude={'secrets'}))
    storage = SecretStore(ctx.paths.home, key)
    fields = {'solo2_password': 'solo2.password', 'github_token': 'github.token', 'gateway_key': 'model.key',
              'source_password': 'projecthub.password', 'source_bearer': 'projecthub.bearer'}
    written = []
    try:
        for field, value in payload.secrets.model_dump(exclude_none=True).items():
            if value:
                written.append(fields[field])
                storage.set(fields[field], value)
        profiles = {**ctx.settings.profiles, key: profile}
        save_settings(ctx.paths, ctx.settings.model_copy(update={'profiles': profiles}))
    except Exception:
        for account in written:
            storage.delete(account)
        raise
    ctx.settings.profiles = profiles
    return profile_catalog(ctx)


@router.post('/{profile_id}/password/{target}', dependencies=[Depends(serialize_account_changes)])
async def reveal_password(profile_id: str, target: PasswordTarget | Literal['github'], request: Request,
                          ctx: AppContext = Depends(context)) -> dict[str, str]:
    if target == 'github':
        require_access(request, ctx)
    if profile_id != ctx.settings.active_profile:
        raise ApiError(409, 'PROFILE_CHANGED', '当前配置已切换，请重新打开账号连接')
    account = {'solo2': 'solo2.password', 'source': 'projecthub.password', 'gateway': 'model.key',
               'github': 'github.token'}[target]
    password = ctx.secrets.get(account)
    if not password:
        raise ApiError(404, 'PASSWORD_UNSET', '当前配置尚未保存此凭据')
    return {'password': password}


@router.post('/{profile_id}/activate', dependencies=[Depends(serialize_account_changes)])
async def activate_profile(profile_id: str, ctx: AppContext = Depends(context)) -> Profiles:
    current = ctx.settings
    target = current.profiles.get(profile_id)
    if not target:
        raise ApiError(404, 'PROFILE_UNKNOWN', '找不到这套配置')
    if profile_id == current.active_profile:
        return profile_catalog(ctx)
    if (ctx.scheduler.active or ctx.scheduler.queue or ctx.dispatcher.in_hand_count() or ctx.intake.updating_plan
            or any(job.active or job.desktop_running for job in ctx.autopilot.jobs.values())):
        raise ApiError(409, 'PROFILE_BUSY', '当前账号还有任务正在处理或排队，请等任务结束后再切换')
    previous_id = current.active_profile
    profiles = {**current.profiles, previous_id: AccountProfile(name=current.profiles[previous_id].name,
                solo2=current.solo2, source=current.source, model_base_url=current.skill.base_url)}
    solo2: Solo2Settings = target.solo2.model_copy(deep=True)
    source_settings: SourceSettings = target.source.model_copy(deep=True)
    skill = current.skill.model_copy(update={'base_url': target.model_base_url})
    storage = SecretStore(ctx.paths.home, profile_id)
    source = ProjectHubClient(source_settings, storage)
    try:
        save_settings(ctx.paths, current.model_copy(update={'profiles': profiles, 'active_profile': profile_id,
                      'solo2': solo2, 'source': source_settings, 'skill': skill}))
    except Exception:
        await source.close()
        raise
    previous = ctx.source
    current.profiles, current.active_profile = profiles, profile_id
    current.solo2, current.source, current.skill = solo2, source_settings, skill
    ctx.secrets = ctx.preflight.secrets = storage
    ctx.solo2 = Solo2Client(solo2, storage)
    ctx.replace_source(source)
    ctx.tasks.staging = ctx.staging = ctx.intake.switch_profile(profile_id)
    await previous.close()
    return profile_catalog(ctx)
