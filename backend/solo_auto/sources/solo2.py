"""Solo2 submission-platform authentication."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from solo_auto.secrets import SecretStore
from solo_auto.settings import Solo2Settings

PASSWORD_ACCOUNT = "solo2.password"


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

    async def test(self) -> dict[str, Any]:
        username = self.settings.username.strip()
        password = self.secrets.get(PASSWORD_ACCOUNT) or ""
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
            message = "Solo2 账号或密码错误"
            try:
                payload = response.json()
                detail = payload.get("detail") if isinstance(payload, dict) else None
                if isinstance(detail, str) and detail.strip():
                    message = detail.strip()
            except ValueError:
                pass
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

        account = username
        try:
            payload = response.json()
            if isinstance(payload, dict):
                nested = payload.get("data")
                candidates = [payload, nested] if isinstance(nested, dict) else [payload]
                for candidate in candidates:
                    user = candidate.get("user")
                    if isinstance(user, dict) and user.get("username"):
                        account = str(user["username"])
                        break
                    if candidate.get("username"):
                        account = str(candidate["username"])
                        break
        except ValueError:
            pass
        return {
            "ok": True,
            "message": f"Solo2 登录成功：{account}",
            "detail": {
                "status": response.status_code,
                "platform": self.settings.base_url,
                "account": account,
            },
        }
