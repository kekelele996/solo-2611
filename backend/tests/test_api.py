from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from solo_auto.app import create_app
from solo_auto.gateway.skill import SkillGateway


@pytest.mark.asyncio
async def test_api_fact_routes_without_real_skill(
    fixture_factory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    for name in ("prompt-only", "running-stale", "exported"):
        fixture_factory(name, tasks_dir / name)
    monkeypatch.setenv("SOLO_AUTO_HOME", str(tmp_path / "app"))
    monkeypatch.setenv("SOLO_AUTO_TASKS_DIR", str(tasks_dir))
    monkeypatch.setenv("SOLO_AUTO_DISABLE_KEYCHAIN", "1")

    async def fake_version(self: SkillGateway) -> str:
        return "0.2.2"

    monkeypatch.setattr(SkillGateway, "version", fake_version)
    app = create_app(tmp_path / "app", auto_start=False)
    context = app.state.context
    await context.startup()
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            health = await client.get("/api/health")
            assert health.status_code == 200
            assert health.json()["ok"] is True

            page = await client.get("/")
            assert page.status_code == 200
            assert page.headers["cache-control"] == "no-cache"

            listing = await client.get("/api/tasks", params={"page_size": 20})
            assert listing.status_code == 200
            assert listing.json()["total"] == 3
            task = next(item for item in listing.json()["items"] if item["task_name"] == "fixture-exported")

            detail = await client.get(f"/api/tasks/{task['id']}")
            assert detail.status_code == 200
            assert detail.json()["complete"] is True
            assert len(detail.json()["steps"]) == 11

            prompt = await client.get(f"/api/tasks/{task['id']}/prompt")
            assert prompt.status_code == 200
            assert prompt.json()["draft"].startswith("真实项目里")
            assert prompt.json()["readonly"] is True

            saved = await client.patch(
                "/api/system/config",
                json={
                    "solo2": {
                        "base_url": "https://solo2.jzxhnh.com/app",
                        "username": "reviewer",
                    },
                    "submission": {
                        "auto_submit": True,
                        "poll_interval_seconds": 120,
                        "poll_timeout_seconds": 3600,
                    },
                    "secrets": {"solo2_password": "saved-password"},
                },
            )
            assert saved.status_code == 200
            assert saved.json()["solo2"]["username"] == "reviewer"
            assert saved.json()["solo2"]["password"] == "<set>"
            assert saved.json()["submission"]["auto_submit"] is True
            assert saved.json()["submission"]["poll_interval_seconds"] == 120
            assert "saved-password" not in saved.text
            assert context.secrets.get("solo2.password") == "saved-password"

            async def fake_solo2_test() -> dict[str, object]:
                return {"ok": True, "message": "Solo2 登录成功：reviewer", "detail": {"status": 200}}

            monkeypatch.setattr(context.solo2, "test", fake_solo2_test)
            solo2_test = await client.post("/api/system/config/test", json={"target": "solo2"})
            assert solo2_test.status_code == 200
            assert solo2_test.json()["ok"] is True

            async def fake_api_probe() -> dict[str, object]:
                return {"ok": False, "message": "HTTP 401：Key 无效", "detail": {"exit_code": 1}}

            monkeypatch.setattr(context.preflight, "probe_api", fake_api_probe)
            api_probe = await client.post("/api/system/config/test", json={"target": "api"})
            assert api_probe.status_code == 200
            assert api_probe.json()["ok"] is False
            assert "401" in api_probe.json()["message"]

            timeline = await client.get(f"/api/tasks/{task['id']}/timeline")
            assert timeline.status_code == 200
            assert timeline.json()["counts"]["total"] == 1

            video = await client.get(f"/api/tasks/{task['id']}/recording/video")
            assert video.status_code == 200
            assert video.headers["content-type"] == "video/mp4"
            assert video.headers["content-disposition"].startswith("inline;")
            assert video.headers["accept-ranges"] == "bytes"

            video_download = await client.get(f"/api/tasks/{task['id']}/recording/video?download=1")
            assert video_download.status_code == 200
            assert video_download.headers["content-disposition"].startswith("attachment;")

            queue = await client.get("/api/queue")
            assert queue.status_code == 200
            assert queue.json()["limit"] == 4

            terminal = await client.post(f"/api/tasks/{task['id']}/record/terminal")
            assert terminal.status_code == 200
            assert terminal.json() == {"ok": True, "window_id": 4242, "title": "solo-2611 录屏 — workspace — zsh -i"}
            missing = await client.post("/api/tasks/no-such-task/record/terminal")
            assert missing.status_code == 404
    finally:
        await context.shutdown()
