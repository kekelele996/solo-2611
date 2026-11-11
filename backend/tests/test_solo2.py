from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from solo_auto.secrets import SecretStore
from solo_auto.settings import Solo2Settings
from solo_auto.sources.solo2 import Solo2Client


@pytest.mark.asyncio
async def test_solo2_login_uses_keychain_password_and_reports_account(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOLO_AUTO_DISABLE_KEYCHAIN", "1")
    secrets = SecretStore(tmp_path / "app")
    secrets.set("solo2.password", "secret-value")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"user": {"username": "reviewer"}})

    client = Solo2Client(
        Solo2Settings(
            base_url="https://solo2.jzxhnh.com/app",
            username="input-user",
        ),
        secrets,
        transport=httpx.MockTransport(handler),
    )
    result = await client.test()

    assert result["ok"] is True
    assert result["message"] == "Solo2 登录成功：reviewer"
    assert result["detail"]["account"] == "reviewer"
    assert str(seen[0].url) == "https://solo2.jzxhnh.com/api/v1/auth/login"
    assert seen[0].headers["origin"] == "https://solo2.jzxhnh.com"
    assert seen[0].headers["referer"] == "https://solo2.jzxhnh.com/app"
    assert json.loads(seen[0].content) == {"username": "input-user", "password": "secret-value"}
    assert "secret-value" not in json.dumps(result, ensure_ascii=False)


@pytest.mark.asyncio
async def test_solo2_login_reports_invalid_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOLO_AUTO_DISABLE_KEYCHAIN", "1")
    secrets = SecretStore(tmp_path / "app")
    secrets.set("solo2.password", "wrong")

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "账号或密码错误"})

    client = Solo2Client(
        Solo2Settings(username="reviewer"),
        secrets,
        transport=httpx.MockTransport(handler),
    )
    result = await client.test()

    assert result["ok"] is False
    assert result["message"] == "账号或密码错误"
    assert result["detail"]["status"] == 401


@pytest.mark.asyncio
async def test_solo2_login_requires_saved_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOLO_AUTO_DISABLE_KEYCHAIN", "1")
    client = Solo2Client(Solo2Settings(), SecretStore(tmp_path / "app"))

    result = await client.test()

    assert result == {
        "ok": False,
        "message": "请先保存 Solo2 平台账号和密码",
        "detail": {},
    }
