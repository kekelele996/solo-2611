"""账号查看码设置、解锁及只读连接摘要。"""
from __future__ import annotations

from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field

from solo_auto.account_access import SESSION_SECONDS
from solo_auto.api.dependencies import context
from solo_auto.context import AppContext
from solo_auto.errors import ApiError

router = APIRouter(prefix='/api/system/accounts/view')


class CodeChange(BaseModel):
    current: str = Field(default='', max_length=128)
    new: str = Field(min_length=6, max_length=128, pattern=r'\S')


class Unlock(BaseModel):
    code: str = Field(min_length=1, max_length=128)
    purpose: Literal['settings', 'dispatch', 'profiles'] = 'dispatch'


PasswordTarget = Literal["solo2", "source", "gateway"]


class Connection(BaseModel):
    name: str
    account: str | None = None
    address: str | None = None
    domain: str | None = None
    credential_saved: bool
    password_target: PasswordTarget | None = None


class View(BaseModel):
    connections: list[Connection]
    expires_in: int
    token: str | None = None


def trusted_skill(request: Request) -> bool:
    return bool(request.client and request.client.host in {'127.0.0.1', '::1'}
                and request.url.hostname in {'127.0.0.1', '::1', 'localhost'}
                and 'origin' not in request.headers and 'sec-fetch-mode' not in request.headers
                and request.headers.get('x-solo-skill') == 'solo-2611')


def require_access(request: Request, ctx: AppContext = Depends(context)) -> None:
    if not ctx.account_access.allows(request.headers.get('x-account-view', '')):
        raise ApiError(403, 'ACCOUNT_LOCKED', '请先输入账号查看码')


def local_page(request: Request) -> None:
    origin = request.headers.get('origin')
    if (not request.client or request.client.host not in {'127.0.0.1', '::1'}
            or request.url.hostname not in {'127.0.0.1', '::1', 'localhost'}
            or (origin and origin != str(request.base_url).rstrip('/'))):
        raise ApiError(403, 'LOCAL_ONLY', '请在本机工作台操作')


@router.get('/status')
async def view_status(response: Response, ctx: AppContext = Depends(context)) -> dict[str, bool]:
    response.headers['Cache-Control'] = 'no-store'
    return {'configured': ctx.account_access.configured}


@router.put('/code', dependencies=[Depends(local_page)], status_code=204)
async def set_view_code(payload: CodeChange, ctx: AppContext = Depends(context)) -> Response:
    await ctx.account_access.configure(payload.current, payload.new)
    return Response(status_code=204, headers={'Cache-Control': 'no-store'})


def display_address(raw: str) -> str:
    # 展示连接目标时去掉地址内可能携带的登录信息与查询参数。
    try:
        parts = urlsplit(raw)
        if not parts.hostname or parts.scheme not in {'http', 'https'}:
            return ''
        return f'{parts.scheme}://{parts.netloc.rsplit("@", 1)[-1]}{parts.path}'
    except ValueError:
        return ''


@router.post('/unlock', dependencies=[Depends(local_page)])
async def unlock(payload: Unlock, response: Response, ctx: AppContext = Depends(context)) -> View:
    token = await ctx.account_access.unlock(payload.code, purpose=payload.purpose)
    response.headers['Cache-Control'] = 'no-store'
    return View(connections=connections(ctx), expires_in=SESSION_SECONDS, token=token)


def connections(ctx: AppContext) -> list[Connection]:
    settings = ctx.settings
    return [
        Connection(name='SOLO2 提交平台', password_target='solo2', account=settings.solo2.username, address=display_address(settings.solo2.base_url),
                   domain=settings.solo2.locked_domain, credential_saved=ctx.secrets.has('solo2.password')),
        Connection(name='GitHub 快照仓库', credential_saved=ctx.secrets.has('github.token')),
        Connection(name='题源账号', password_target='source', account=settings.source.username, address=display_address(settings.source.base_url),
                   credential_saved=ctx.secrets.has('projecthub.password')),
        Connection(name='模型网关', password_target='gateway', address=display_address(settings.skill.base_url),
                   credential_saved=ctx.secrets.has('model.key')),
    ]


@router.post('/lock', dependencies=[Depends(local_page)], status_code=204)
async def lock_view(request: Request, ctx: AppContext = Depends(context)) -> Response:
    ctx.account_access.sessions.pop(request.headers.get('x-account-view', ''), None)
    return Response(status_code=204)
