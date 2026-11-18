"""Verify a GitHub token and report the account it belongs to."""

from typing import Any

import httpx


async def test_credentials(token: str | None) -> dict[str, Any]:
    if not token:
        return {"ok": False, "message": "请先填写或保存 GitHub 凭据", "detail": {}}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get("https://api.github.com/user", headers={"Authorization": f"Bearer {token}"})
        if response.status_code != 200:
            return {"ok": False, "message": f"GitHub 认证失败（HTTP {response.status_code}）", "detail": {}}
        body = response.json()
        account = body.get("login") if isinstance(body, dict) else None
        if not isinstance(account, str) or not account:
            return {"ok": False, "message": "GitHub 未返回账号，身份未核验", "detail": {}}
        return {"ok": True, "message": f"GitHub 连接成功：{account}", "detail": {}}
    except (ValueError, httpx.HTTPError):
        return {"ok": False, "message": "GitHub 连接失败，请检查网络或凭据格式", "detail": {}}
