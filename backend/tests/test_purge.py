from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from solo_auto.app import create_app
from solo_auto.gateway.skill import SkillGateway


@pytest.mark.asyncio
async def test_purge_previews_then_trashes_or_deletes(
    fixture_factory: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    roots = {name: fixture_factory(name, tasks_dir / name) for name in ("prompt-only", "running-stale", "exported")}
    monkeypatch.setenv("SOLO_AUTO_HOME", str(tmp_path / "app"))
    monkeypatch.setenv("SOLO_AUTO_TASKS_DIR", str(tasks_dir))

    async def fake_version(self: SkillGateway) -> str:
        return "0.2.3"

    monkeypatch.setattr(SkillGateway, "version", fake_version)
    app = create_app(tmp_path / "app", auto_start=False)
    ctx = app.state.context
    await ctx.startup()
    ids = {name: ctx.repo.get_task_by_root(root)["id"] for name, root in roots.items()}
    drafts = ctx.paths.drafts / ids["prompt-only"]
    drafts.mkdir(parents=True)
    (drafts / "题面草稿.md").write_text("草稿", encoding="utf-8")
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.post("/api/tasks/purge", json={"task_ids": []})).status_code == 400
            assert (await client.post("/api/tasks/purge", json={"task_ids": ["nope"]})).status_code == 404

            preview = (await client.post("/api/tasks/purge", json={"task_ids": list(ids.values())})).json()
            assert preview["dry_run"] is True and len(preview["items"]) == 3
            by_name = {item["task_id"]: item for item in preview["items"]}
            assert str(drafts) in by_name[ids["prompt-only"]]["paths"]
            assert any("已经导出过" in text for text in by_name[ids["exported"]]["warnings"])
            assert any("GitHub 仓库不会被删除" in text for text in by_name[ids["exported"]]["warnings"])
            assert all(root.is_dir() for root in roots.values())
            assert (await client.get("/api/tasks")).json()["total"] == 3

            # 自动交付进行中的任务不能清理
            ctx.autopilot.agent_command = ["/bin/sh"]
            await ctx.autopilot.enroll([ids["running-stale"]])
            body = {"task_ids": [ids["prompt-only"], ids["running-stale"]], "dry_run": False}
            done = (await client.post("/api/tasks/purge", json=body)).json()
            first, second = done["items"]
            assert first["removed"] is True and Path(first["moved_to"]).is_relative_to(ctx.paths.trash)
            assert (Path(first["moved_to"]) / "0-prompt-only" / "monitor" / "state.json").is_file()
            assert not roots["prompt-only"].exists() and not drafts.exists()
            assert second["blockers"] == ["自动交付进行中，请先停止"] and "removed" not in second
            assert roots["running-stale"].is_dir()

            await ctx.autopilot.cancel()
            body = {"task_ids": [ids["running-stale"]], "dry_run": False, "permanent": True}
            gone = (await client.post("/api/tasks/purge", json=body)).json()["items"][0]
            assert gone["removed"] is True and gone["moved_to"] == ""
            assert not roots["running-stale"].exists()
            assert ids["running-stale"] not in ctx.autopilot.jobs

            listing = (await client.get("/api/tasks")).json()
            assert [item["task_name"] for item in listing["items"]] == ["fixture-exported"]
            await ctx.reconciler.run()
            assert (await client.get("/api/tasks")).json()["total"] == 1
            assert (await client.get(f"/api/tasks/{ids['prompt-only']}")).status_code == 404
    finally:
        await ctx.shutdown()
