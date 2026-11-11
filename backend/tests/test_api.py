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

            timeline = await client.get(f"/api/tasks/{task['id']}/timeline")
            assert timeline.status_code == 200
            assert timeline.json()["counts"]["total"] == 1

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
