from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from solo_auto.pipeline.preflight import Preflight


@pytest.mark.asyncio
async def test_api_probe_uses_skill_config_test_and_reports_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preflight = Preflight.__new__(Preflight)
    calls: list[list[str]] = []

    async def fake_call(argv: list[str], **_: Any) -> SimpleNamespace:
        calls.append(argv)
        return SimpleNamespace(code=0, out="✅ https://gateway.test/v1/messages -> HTTP 200", err="")

    monkeypatch.setattr(preflight, "gateway", SimpleNamespace(call=fake_call), raising=False)
    result = await preflight.probe_api()

    assert result["ok"] is True
    assert result["detail"]["exit_code"] == 0
    assert "HTTP 200" in result["message"]
    assert calls == [["config", "test"]]


@pytest.mark.asyncio
async def test_api_probe_blocks_on_skill_test_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preflight = Preflight.__new__(Preflight)

    async def fake_call(_: list[str], **__: Any) -> SimpleNamespace:
        return SimpleNamespace(code=1, out="", err="HTTP 401：Key 无效")

    monkeypatch.setattr(preflight, "gateway", SimpleNamespace(call=fake_call), raising=False)
    result = await preflight.probe_api()

    assert result["ok"] is False
    assert result["detail"]["exit_code"] == 1
    assert "401" in result["message"]
