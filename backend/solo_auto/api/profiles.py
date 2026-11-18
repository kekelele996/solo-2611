"""Named account configurations. Only the active profile is used by workbench clients."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field, field_validator

from solo_auto.account_binding import origin, standby_record
from solo_auto.account_binding import standby_key as _standby_key
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
from solo_auto.errors import ApiError, SourceAuthError, SourceOfflineError
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


class PoolSource(BaseModel):
    standby_use_primary_source: bool = False
    standby_source_address: str = Field(default='', max_length=2000)
    standby_source_user: str = Field(default='', max_length=200)
    standby_source_password: str = ''


class PoolUpdate(PoolSource):
    primary_project_id: str = Field(min_length=1, max_length=200, pattern=r'\S')
    standby_project_id: str = Field(min_length=1, max_length=200)


def _require_pool_settings(profile_id: str, request: Request, ctx: AppContext) -> None:
    if profile_id != ctx.settings.active_profile:
        raise ApiError(409, 'PROFILE_CHANGED', '当前用户配置已切换，请重新打开工程池设置')
    if not ctx.account_access.unlocked(request.headers.get('x-account-view', ''), settings=True):
        raise ApiError(403, 'ACCOUNT_LOCKED', '请先输入查看码解锁副号项目池')


def _standby_pool_source(payload: PoolSource, ctx: AppContext) -> tuple[ProjectHubClient, str]:
    if payload.standby_use_primary_source:
        return ctx.source, ''
    address = payload.standby_source_address.strip().rstrip('/')
    user = payload.standby_source_user.strip()
    if not user:
        raise ApiError(422, 'POOL_SOURCE_USER', '请填写副号工程台账号')
    try:
        url = urlsplit(address)
        if url.scheme not in {'http', 'https'} or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError
        _ = url.port
    except ValueError:
        raise ApiError(422, 'SOURCE_URL', '请填写工程台的 HTTP 或 HTTPS 地址') from None
    if origin(address) == origin(ctx.settings.source.base_url) and user == ctx.settings.source.username:
        raise ApiError(422, 'POOL_ACCOUNT_OVERLAP', '主号和副号须使用各自的工程台账号')
    saved = ctx.routing.config
    same_source = (address, user) == (saved.get('standby_source_address'), saved.get('standby_source_user'))
    password = payload.standby_source_password or (str(saved.get('standby_source_password') or '') if same_source else '')
    if not password:
        raise ApiError(422, 'POOL_SOURCE_PASSWORD', '请填写该副号的工程台密码')
    return ProjectHubClient(SourceSettings(base_url=address, username=user), ctx.secrets, password=password), password


async def _verified_projects(source: ProjectHubClient) -> dict[str, str]:
    try:
        await source.verify_identity()
        projects = await source.list_projects(refresh=True)
    except (SourceAuthError, SourceOfflineError) as exc:
        raise ApiError(409, 'POOL_SOURCE_UNAVAILABLE', str(exc)) from exc
    return {str(project['id']): str(project.get('name') or project['id']) for project in projects if project.get('id')}


@router.get('/{profile_id}/engineering-pools')
async def get_engineering_pools(profile_id: str, request: Request, ctx: AppContext = Depends(context)) -> dict[str, Any]:
    _require_pool_settings(profile_id, request, ctx)
    return ctx.routing.pool_view()


@router.post('/{profile_id}/engineering-pools/projects', dependencies=[Depends(serialize_account_changes)])
async def list_standby_projects(profile_id: str, payload: PoolSource, request: Request,
                                ctx: AppContext = Depends(context)) -> dict[str, Any]:
    _require_pool_settings(profile_id, request, ctx)
    source, _ = _standby_pool_source(payload, ctx)
    try:
        projects = await _verified_projects(source)
    finally:
        if source is not ctx.source:
            await source.close()
    primary, _ = ctx.routing.primary_project()
    return {'items': [{'id': project_id, 'name': name} for project_id, name in projects.items() if project_id != primary]}


@router.put('/{profile_id}/engineering-pools', dependencies=[Depends(serialize_account_changes)])
async def save_engineering_pools(profile_id: str, payload: PoolUpdate, request: Request,
                                 ctx: AppContext = Depends(context)) -> dict[str, Any]:
    _require_pool_settings(profile_id, request, ctx)
    primary_project, _ = ctx.routing.primary_project()
    if not primary_project or payload.primary_project_id != primary_project:
        raise ApiError(409, 'POOL_PRIMARY_CHANGED', '主号项目池未设置或已变更，请核对题源页上的当前项目')
    ctx.routing.check_project_groups(primary_project, payload.standby_project_id)
    standby = standby_credentials(ctx)
    if not standby or not ctx.settings.solo2.locked_domain:
        raise ApiError(409, 'POOL_ACCOUNTS_REQUIRED', '请先设置主副号的 SOLO2、绑定领域和 GitHub 凭据')
    if ctx.intake.updating_plan:
        raise ApiError(409, 'INTAKE_BUSY', '正在领取工程，请稍后保存')
    source, password = _standby_pool_source(payload, ctx)
    try:
        standby_projects, primary_github, standby_github = await asyncio.gather(
            _verified_projects(source),
            github.test_credentials(ctx.secrets.get('github.token')), github.test_credentials(standby.github_token),
        )
        standby_name = standby_projects.get(payload.standby_project_id)
        if not standby_name:
            raise ApiError(422, 'POOL_PROJECT_FORBIDDEN', '工程台账号无法读取所选的副号项目组')
        primary_owner = str((primary_github.get('detail') or {}).get('account') or '')
        standby_owner = str((standby_github.get('detail') or {}).get('account') or '')
        if not primary_owner or not standby_owner or primary_owner.casefold() == standby_owner.casefold():
            raise ApiError(422, 'POOL_GITHUB_IDENTITY', '请为主副号提供可登录且身份不同的 GitHub 凭据')
        saved = dict(ctx.routing.config)
        updated = {**saved,
            'standby_project_id': payload.standby_project_id, 'standby_project_name': standby_name,
            'standby_source_user': source.settings.username, 'standby_source_address': source.settings.base_url,
            'standby_source_password': password, 'standby_account': standby.username, 'standby_github_owner': standby_owner,
            'standby_use_primary_source': payload.standby_use_primary_source,
        }
        async with ctx.intake._lock:
            if ctx.routing.primary_project()[0] != primary_project:
                raise ApiError(409, 'POOL_PRIMARY_CHANGED', '主号项目池已变更，请刷新后再保存')
            if ctx.routing.busy('standby') and any(saved.get(f'standby_{key}') != updated.get(f'standby_{key}')
                                                   for key in ('project_id', 'source_user', 'source_address', 'github_owner')):
                raise ApiError(409, 'ROUTED_TASKS_ACTIVE', '副号项目池仍有关联任务或待归还工程，请处理完成后再更换')
            ctx.secrets.set_json(ctx.routing.secret_key, updated)
            if ctx.secrets.get_json(ctx.routing.secret_key) != updated:
                raise ApiError(500, 'POOL_SAVE_FAILED', '工程池保存后核对失败')
            await ctx.routing.invalidate()
        ctx.repo.add_event('dispatch.engineering_pools_saved', '已保存副号项目池', actor='user')
        return ctx.routing.pool_view()
    finally:
        if source is not ctx.source:
            await source.close()


@router.get('')
async def list_profiles(ctx: AppContext = Depends(context)) -> Profiles:
    return profile_catalog(ctx)


def profile_catalog(ctx: AppContext) -> Profiles:
    settings = ctx.settings
    record = ctx.secrets.get_json(_standby_key(settings.solo2))
    return Profiles(active=settings.active_profile, items=[ProfileItem(id=k, name=v.name) for k, v in settings.profiles.items()],
                    connections=connections(ctx), standby=StandbyAccount.model_validate(record) if record else None)


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
    current = standby_record(ctx) or {}
    if (current.get('username'), current.get('domain')) != (draft.username, draft.domain) and ctx.routing.busy('standby'):
        raise ApiError(409, 'ROUTED_TASKS_ACTIVE', '副号仍有在途任务或待归还工程，请完成后再更换账号或领域')
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
    if (ctx.scheduler.active or ctx.scheduler.queue or ctx.dispatcher.in_hand_count() or ctx.intake.updating_plan or ctx.routing.busy()
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
    await ctx.routing.invalidate()
    await previous.close()
    return profile_catalog(ctx)
