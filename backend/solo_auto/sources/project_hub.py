"""HTTP adapter for the local Project Hub."""

from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import suppress
from pathlib import Path
from typing import Any, cast

import httpx

from solo_auto.errors import ApiError, SourceAuthError, SourceOfflineError
from solo_auto.secrets import SecretStore
from solo_auto.settings import SourceSettings


class ProjectHubClient:
    def __init__(
        self,
        settings: SourceSettings,
        secrets: SecretStore,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.secrets = secrets
        self._csrf = secrets.get("projecthub.csrf") or ""
        self._persisted_csrf = self._csrf
        self._client = httpx.AsyncClient(
            base_url=settings.base_url,
            timeout=httpx.Timeout(settings.timeout_seconds),
            follow_redirects=True,
            headers={"Accept": "application/json"},
            transport=transport,
        )
        cookies = secrets.get_json("projecthub.cookies", {})
        self._cookie_signature = json.dumps(cookies, sort_keys=True) if isinstance(cookies, dict) else ""
        if isinstance(cookies, dict):
            for name, value in cookies.items():
                self._client.cookies.set(str(name), str(value), domain=httpx.URL(settings.base_url).host)

    async def close(self) -> None:
        await self._client.aclose()

    async def test(self) -> dict[str, Any]:
        try:
            await self.list_projects(refresh=True)
            return {"ok": True, "message": "工程台连接正常", "detail": {}}
        except SourceAuthError as exc:
            return {"ok": False, "message": str(exc), "detail": {"status": 401}}
        except SourceOfflineError as exc:
            return {"ok": False, "message": str(exc), "detail": {}}

    async def login(self, username: str | None = None, password: str | None = None) -> dict[str, Any]:
        user = username if username is not None else self.settings.username
        secret = password if password is not None else self.secrets.get("projecthub.password")
        if not user or not secret:
            raise SourceAuthError("工程台用户名或密码未配置")
        response = await self._request(
            "POST",
            "/api/v1/auth/login",
            json={"username": user, "password": secret},
            csrf=False,
            allow_reauth=False,
        )
        data = _json(response)
        self._csrf = str(
            data.get("csrf")
            or data.get("csrf_token")
            or data.get("csrfToken")
            or response.headers.get("X-CSRF-Token")
            or ""
        )
        self._persist_session()
        return cast(dict[str, Any], data)

    async def list_projects(self, *, refresh: bool = False) -> list[dict[str, Any]]:
        await self._ensure_session()
        response = await self._request(
            "GET",
            "/api/v1/me/projects",
            params={"page": 1, "page_size": 100},
            cache_bust=refresh,
        )
        data = _json(response)
        items = data.get("items") if isinstance(data, dict) else data
        return [item for item in items or [] if isinstance(item, dict)]

    async def list_engineering(
        self,
        project_id: str,
        *,
        q: str = "",
        language: str = "",
        min_files: int | None = None,
        max_files: int | None = None,
        tag_ids: list[str] | None = None,
        scope: str = "available",
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        if not project_id.strip():
            raise ApiError(400, "PROJECT_REQUIRED", "请先选择项目")
        await self._ensure_session()
        params: list[tuple[str, Any]] = [
            ("scope", scope),
            ("q", q),
            ("language", language),
            ("min_files", min_files),
            ("max_files", max_files),
            ("page", max(1, page)),
            ("page_size", max(1, min(page_size, 200))),
        ]
        for tag in tag_ids or []:
            params.append(("tag_ids", tag))
        response = await self._request(
            "GET",
            f"/api/v1/projects/{project_id}/engineering",
            params=[(key, value) for key, value in params if value not in {None, ""}],
        )
        data = _json(response)
        if not isinstance(data, dict):
            return {"items": [], "total": 0, "page": page, "page_size": page_size}
        data.setdefault("items", [])
        data.setdefault("total", len(data["items"]))
        data.setdefault("page", page)
        data.setdefault("page_size", page_size)
        return data

    async def detail(self, project_id: str, engineering_id: str) -> dict[str, Any]:
        page = await self.list_engineering(project_id, scope="all", page_size=200)
        for item in page.get("items", []):
            if _engineering_id(item) == engineering_id:
                return cast(dict[str, Any], item)
        raise ApiError(404, "ENGINEERING_NOT_FOUND", f"工程不存在或不在项目内: {engineering_id}")

    async def claim(self, project_id: str, engineering_id: str) -> dict[str, Any]:
        await self._ensure_session()
        before = await self.detail(project_id, engineering_id)
        response = await self._request(
            "POST",
            f"/api/v1/projects/{project_id}/engineering/{engineering_id}/claim",
            json={},
        )
        try:
            data = _json(response)
        except json.JSONDecodeError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        merged = dict(before)
        merged.update(data)
        if not _claim_id(merged):
            with suppress(Exception):
                refreshed = await self.detail(project_id, engineering_id)
                merged.update(refreshed)
        merged.setdefault("claim_id", _claim_id(merged))
        merged["engineering_id"] = engineering_id
        return merged

    async def release(self, project_id: str, engineering_id: str, claim_id: str) -> dict[str, Any]:
        await self._ensure_session()
        response = await self._request(
            "POST",
            f"/api/v1/projects/{project_id}/engineering/{engineering_id}/release",
            json={"claim_id": claim_id} if claim_id else {},
        )
        if not response.content:
            return {"ok": True}
        data = _json(response)
        return data if isinstance(data, dict) else {"ok": True}

    async def download_zip(self, project_id: str, engineering_id: str, target: Path) -> str:
        await self._ensure_session()
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_suffix(target.suffix + ".part")
        last_error: Exception | None = None
        for attempt in range(3):
            temp.unlink(missing_ok=True)
            digest = hashlib.sha256()
            try:
                async with self._client.stream(
                    "GET",
                    f"/api/v1/projects/{project_id}/engineering/{engineering_id}/zip",
                ) as response:
                    self._raise_for_status(response, "ZIP 下载")
                    with temp.open("wb") as handle:
                        async for chunk in response.aiter_bytes(1024 * 512):
                            digest.update(chunk)
                            handle.write(chunk)
                temp.replace(target)
                return digest.hexdigest()
            except (httpx.HTTPError, OSError) as exc:
                last_error = exc
                temp.unlink(missing_ok=True)
                if attempt < 2:
                    await asyncio.sleep(0.25 * (2**attempt))
        raise SourceOfflineError(f"ZIP 下载连续 3 次失败：{last_error}")

    async def fetch_asset(self, asset_id: str) -> tuple[bytes, str]:
        await self._ensure_session()
        response = await self._request("GET", f"/api/v1/assets/{asset_id}")
        return response.content, response.headers.get("content-type", "application/octet-stream")

    async def _ensure_session(self) -> None:
        if self._client.cookies.jar:
            return
        if self.settings.username and self.secrets.get("projecthub.password"):
            await self.login()
            return
        raise SourceAuthError("工程台会话已失效，请重新登录")

    async def _request(
        self,
        method: str,
        path: str,
        *,
        csrf: bool = True,
        allow_reauth: bool = True,
        cache_bust: bool = False,
        **kwargs: Any,
    ) -> httpx.Response:
        headers = dict(kwargs.pop("headers", {}))
        if csrf and method not in {"GET", "HEAD"} and self._csrf:
            headers["X-CSRF-Token"] = self._csrf
        if cache_bust:
            params = dict(kwargs.get("params") or {})
            params["_"] = str(int(asyncio.get_running_loop().time() * 1000))
            kwargs["params"] = params
        try:
            response = await self._client.request(method, path, headers=headers, **kwargs)
        except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError) as exc:
            raise SourceOfflineError(f"题源不可达：{exc}") from exc
        if response.status_code == 401 and allow_reauth and path != "/api/v1/auth/login":
            self._csrf = ""
            self._client.cookies.clear()
            try:
                await self.login()
                response = await self._client.request(method, path, headers=headers, **kwargs)
            except SourceAuthError:
                raise
        self._persist_session()
        self._raise_for_status(response, f"{method} {path}")
        return response

    def _raise_for_status(self, response: httpx.Response, action: str) -> None:
        if response.is_success:
            return
        try:
            payload = response.json()
            message = str(payload.get("error") or payload.get("message") or payload.get("detail") or payload)
        except (json.JSONDecodeError, ValueError):
            message = response.text[:1000] or f"HTTP {response.status_code}"
        if response.status_code == 401:
            raise SourceAuthError(f"{action} 返回 401：{message}")
        if response.status_code == 409:
            raise ApiError(409, "ENGINEERING_ALREADY_CLAIMED", f"{action} 冲突：{message}")
        if response.status_code == 404:
            raise ApiError(404, "SOURCE_NOT_FOUND", f"{action} 返回 404：{message}")
        if response.status_code == 403:
            raise ApiError(403, "SOURCE_FORBIDDEN", f"{action} 返回 403：{message}")
        raise ApiError(response.status_code, "SOURCE_ERROR", f"{action} 失败：{message}")

    def _persist_session(self) -> None:
        cookies = {cookie.name: cookie.value for cookie in self._client.cookies.jar}
        signature = json.dumps(cookies, sort_keys=True)
        if signature == self._cookie_signature and self._persisted_csrf == self._csrf:
            return
        self.secrets.set_json("projecthub.cookies", cookies)
        self.secrets.set("projecthub.csrf", self._csrf)
        self._cookie_signature = signature
        self._persisted_csrf = self._csrf


def _json(response: httpx.Response) -> Any:
    if not response.content:
        return {}
    return response.json()


def _engineering_id(item: dict[str, Any]) -> str:
    return str(item.get("engineering_id") or item.get("id") or "")


def _claim_id(item: dict[str, Any]) -> str:
    direct = item.get("claim_id") or item.get("claimId")
    if direct:
        return str(direct)
    claim = item.get("claim")
    if isinstance(claim, dict):
        return str(claim.get("id") or "")
    return ""
