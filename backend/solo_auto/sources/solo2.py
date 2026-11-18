"""Solo2 submission-platform authentication."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import httpx

from solo_auto.secrets import SecretStore
from solo_auto.settings import Solo2Settings

PASSWORD_ACCOUNT = "solo2.password"
SHANGHAI = ZoneInfo("Asia/Shanghai")
DISCARDED_STATUSES = {"DISCARDED", "QC_DISCARDED"}


@dataclass
class DailySubmissions:
    ids: set[str] = field(default_factory=set)
    discarded_ids: set[str] = field(default_factory=set)


def submission_day(value: Any) -> date:
    moment = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    return moment.astimezone(SHANGHAI).date() if moment.tzinfo else moment.date()


def _origin_and_api(base_url: str) -> tuple[str, str]:
    parsed = urlsplit(base_url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Solo2 平台地址必须是完整的 http/https 地址")
    origin = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
    return origin, f"{origin}/api/v1/auth/login"


class Solo2Client:
    def __init__(
        self,
        settings: Solo2Settings,
        secrets: SecretStore,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.secrets = secrets
        self.transport = transport

    async def first_submissions(self, day: date, *, password: str | None = None,
                                local_ids: set[str] | None = None) -> DailySubmissions:
        session = await self.login_session(password=password)
        if not session.get("ok"):
            raise ValueError(str(session.get("message") or "SOLO2 登录失败"))
        headers = {"Accept": "application/json", "Cookie": session["cookie"], "X-CSRF-Token": session["csrf"]}
        async with httpx.AsyncClient(base_url=session["server"], headers=headers, transport=self.transport,
                                     timeout=self.settings.timeout_seconds) as client:
            user = _payload(await client.get("/api/v1/auth/me"))
            user = user.get("user", user)
            if user.get("username") != session["account"] or not user.get("id"):
                raise ValueError("SOLO2 统计账号身份未核验")
            account_id = str(user["id"])
            submissions = DailySubmissions()
            unseen = {value for value in local_ids or () if value.isascii() and value.isdecimal()}
            page = 1
            semaphore = asyncio.Semaphore(5)

            def record(row: dict[str, Any], first_day: date) -> None:
                if first_day == day:
                    submission_id = str(row["id"])
                    submissions.ids.add(submission_id)
                    if str(row.get("status") or "").upper() in DISCARDED_STATUSES:
                        submissions.discarded_ids.add(submission_id)

            def detail_day(detail: dict[str, Any]) -> date:
                event = next((item for item in detail.get("timeline", []) if item.get("action") == "SUBMIT"), None)
                return submission_day((event or {}).get("created_at") or detail.get("created_at"))

            async def first_day(row: dict[str, Any]) -> date:
                if str(row.get("current_version")) == "1":
                    return submission_day(row.get("submitted_at"))
                async with semaphore:
                    detail = _payload(await client.get(f"/api/v1/submissions/{int(row['id'])}"))
                return detail_day(detail)

            async def confirm_local(submission_id: str) -> None:
                async with semaphore:
                    response = await client.get(f"/api/v1/submissions/{submission_id}")
                if response.status_code in {403, 404}:
                    return
                detail = _payload(response)
                if str(detail.get("user_id")) == account_id and str(detail.get("id")) == submission_id and detail.get("status"):
                    record(detail, detail_day(detail))

            while True:
                data = _payload(await client.get("/api/v1/submissions", params={
                    "page": page, "page_size": 100, "user_id": account_id,
                    "date_from": day.isoformat(), "date_to": day.isoformat(),
                }))
                items, meta = data.get("items"), data.get("meta")
                if not isinstance(items, list) or not isinstance(meta, dict) or "total_pages" not in meta:
                    raise ValueError("SOLO2 提交列表分页不完整")
                if any(str(row.get("user_id")) != account_id or not row.get("id") or not row.get("status") for row in items):
                    raise ValueError("SOLO2 提交列表含未核验的账号记录或提交状态")
                dates = await asyncio.gather(*(first_day(row) for row in items))
                for row, first in zip(items, dates, strict=True):
                    unseen.discard(str(row["id"]))
                    record(row, first)
                if page >= int(meta["total_pages"]):
                    await asyncio.gather(*(confirm_local(submission_id) for submission_id in unseen))
                    return submissions
                if not items:
                    raise ValueError("SOLO2 提交列表缺页")
                page += 1

    async def test(self, *, password: str | None = None) -> dict[str, Any]:
        username = self.settings.username.strip()
        if password is None:
            password = self.secrets.get(PASSWORD_ACCOUNT)
        if not username or not password:
            return {
                "ok": False,
                "message": "请先保存 Solo2 平台账号和密码",
                "detail": {},
            }
        try:
            origin, login_url = _origin_and_api(self.settings.base_url)
        except ValueError as exc:
            return {"ok": False, "message": str(exc), "detail": {}}

        headers = {
            "Accept": "application/json, text/plain, */*",
            "Origin": origin,
            "Referer": self.settings.base_url,
            "User-Agent": "solo-auto/0.2",
        }
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(self.settings.timeout_seconds),
                follow_redirects=True,
                transport=self.transport,
            ) as client:
                response = await client.post(
                    login_url,
                    json={"username": username, "password": password},
                    headers=headers,
                )
        except httpx.HTTPError as exc:
            return {
                "ok": False,
                "message": f"无法连接 Solo2：{exc.__class__.__name__}",
                "detail": {},
            }

        if response.status_code in {401, 403}:
            message = "账号或密码错误"
            return {
                "ok": False,
                "message": message,
                "detail": {"status": response.status_code},
            }
        if not response.is_success:
            return {
                "ok": False,
                "message": f"Solo2 登录失败（HTTP {response.status_code}）",
                "detail": {"status": response.status_code},
            }

        account = ""
        domain = ""
        try:
            payload = response.json()
            if isinstance(payload, dict):
                nested = payload.get("data")
                candidates = [payload, nested] if isinstance(nested, dict) else [payload]
                for candidate in candidates:
                    user = candidate.get("user")
                    if isinstance(user, dict) and user.get("username"):
                        domain = str(user.get("domain") or "")
                        account = str(user["username"])
                        break
                    if candidate.get("username"):
                        domain = str(candidate.get("domain") or "")
                        account = str(candidate["username"])
                        break
        except ValueError:
            pass
        if account != username:
            return {"ok": False, "message": "平台返回的账号与配置不一致或身份未核验", "detail": {"status": response.status_code}}
        if domain and self.settings.locked_domain and domain != self.settings.locked_domain:
            return {"ok": False, "message": f"平台领域为{domain}，与本地绑定不一致", "detail": {"account": account}}
        return {
            "ok": True,
            "message": f"Solo2 登录成功：{account}。" + (
                "账号领域已核验" if domain and domain == self.settings.locked_domain else
                f"平台领域：{domain}，请核对账号绑定" if domain else "平台未返回领域，绑定未核验"),
            "detail": {
                "status": response.status_code,
                "platform": self.settings.base_url,
                "account": account,
            },
        }

    async def login_session(self, *, password: str | None = None) -> dict[str, Any]:
        """Return one in-memory SOLO2 session derived only from workbench settings.

        The workbench is the sole owner of the configured username, platform URL
        and stored password. The returned account must exactly match the
        configured username; otherwise callers must stop rather than reuse a
        stale or cross-account session.
        """
        username = self.settings.username.strip()
        if password is None:
            password = self.secrets.get(PASSWORD_ACCOUNT)
        if not username or not password:
            return {
                "ok": False,
                "message": "请先保存 Solo2 平台账号和密码",
                "detail": {},
            }
        try:
            origin, login_url = _origin_and_api(self.settings.base_url)
        except ValueError as exc:
            return {"ok": False, "message": str(exc), "detail": {}}

        headers = {
            "Accept": "application/json, text/plain, */*",
            "Origin": origin,
            "Referer": self.settings.base_url,
            "User-Agent": "solo-auto/0.2",
        }
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(self.settings.timeout_seconds),
                follow_redirects=True,
                transport=self.transport,
            ) as client:
                response = await client.post(
                    login_url,
                    json={"username": username, "password": password},
                    headers=headers,
                )
        except httpx.HTTPError as exc:
            return {
                "ok": False,
                "message": f"无法连接 Solo2：{exc.__class__.__name__}",
                "detail": {},
            }

        if response.status_code in {401, 403}:
            return {
                "ok": False,
                "message": "Solo2 账号或密码错误",
                "detail": {"status": response.status_code},
            }
        if not response.is_success:
            return {
                "ok": False,
                "message": f"Solo2 登录失败（HTTP {response.status_code}）",
                "detail": {"status": response.status_code},
            }

        try:
            payload = response.json()
        except ValueError:
            return {
                "ok": False,
                "message": "Solo2 登录响应不是 JSON",
                "detail": {"status": response.status_code},
            }
        csrf = str(payload.get("csrf_token") or "").strip() if isinstance(payload, dict) else ""
        account = ""
        if isinstance(payload, dict):
            nested = payload.get("data")
            candidates = [payload, nested] if isinstance(nested, dict) else [payload]
            for candidate in candidates:
                user = candidate.get("user")
                if isinstance(user, dict) and user.get("username"):
                    account = str(user["username"]).strip()
                    break
                if candidate.get("username"):
                    account = str(candidate["username"]).strip()
                    break
        cookie = "; ".join(f"{name}={response.cookies[name]}" for name in response.cookies)
        if not csrf or not cookie:
            return {
                "ok": False,
                "message": "Solo2 登录响应缺少 Cookie 或 CSRF",
                "detail": {"status": response.status_code},
            }
        if not account:
            return {
                "ok": False,
                "message": "Solo2 登录响应未返回真实账号，无法验证身份",
                "detail": {"status": response.status_code},
            }
        if account != username:
            return {
                "ok": False,
                "message": f"Solo2 登录账号不一致：配置 {username}，平台返回 {account}",
                "detail": {
                    "status": response.status_code,
                    "configuredAccount": username,
                    "platformAccount": account,
                },
            }
        return {
            "ok": True,
            "account": account,
            "server": origin,
            "cookie": cookie,
            "csrf": csrf,
        }


def _payload(response: httpx.Response) -> dict[str, Any]:
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError("SOLO2 响应不是对象")
    return data
