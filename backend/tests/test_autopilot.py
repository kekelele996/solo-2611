from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

from solo_auto.app import create_app
from solo_auto.gateway.skill import SkillGateway
from solo_auto.pipeline.autopilot import build_prompt

FAKE_AGENT = """
import json, sys
print(json.dumps({"type": "thread.started", "thread_id": "t"}))
print(json.dumps({"type": "item.completed", "item": {"id": "1", "type": "command_execution",
      "command": "python3 solo2611.py status", "aggregated_output": "3/11", "exit_code": 0}}))
print(json.dumps({"type": "item.completed", "item": {"id": "2", "type": "agent_message",
      "text": "题面门禁没过\\nAUTOPILOT: FAILED 难度论证不足"}}))
"""


async def _app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fixture_factory: Any, name: str) -> tuple[Any, Path]:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    root = fixture_factory(name, tasks_dir / name)
    monkeypatch.setenv("SOLO_AUTO_HOME", str(tmp_path / "app"))
    monkeypatch.setenv("SOLO_AUTO_TASKS_DIR", str(tasks_dir))

    async def fake_version(self: SkillGateway) -> str:
        return "0.2.3"

    monkeypatch.setattr(SkillGateway, "version", fake_version)
    app = create_app(tmp_path / "app", auto_start=False)

    async def fake_preflight() -> dict[str, Any]:
        return {"ok": True, "items": [], "checked_at": "2026-11-11T00:00:00+08:00"}

    app.state.context.preflight.run = fake_preflight
    return app, root


@pytest.mark.asyncio
async def test_autopilot_launches_agent_and_fails_honestly(
    fixture_factory: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root = await _app(tmp_path, monkeypatch, fixture_factory, "prompt-only")
    ctx = app.state.context
    await ctx.startup()
    agent = tmp_path / "agent.py"
    agent.write_text(FAKE_AGENT, encoding="utf-8")
    pilot = ctx.autopilot
    pilot.agent_command = [sys.executable, str(agent)]
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            task_id = (await client.get("/api/tasks")).json()["items"][0]["id"]
            assert (await client.post("/api/autopilot/start", json={"task_ids": []})).status_code == 422
            assert (await client.post("/api/autopilot/start", json={"task_ids": ["nope"]})).status_code == 404
            started = await client.post("/api/autopilot/start", json={"task_ids": [task_id]})
            assert started.status_code == 200 and started.json()["added"] == [task_id]

            for attempt in (1, 2):
                await pilot.tick()
                job = pilot.jobs[task_id]
                assert job.stage == "authoring" and job.attempts["authoring"] == attempt, job.detail
                await pilot.agents[task_id].waiter
                assert "难度论证不足" in job.detail
            await pilot.tick()
            assert pilot.jobs[task_id].stage == "failed"
            assert "已尝试 2 次" in pilot.jobs[task_id].detail

            log = (await client.get(f"/api/autopilot/{task_id}/log")).json()
            assert [line["kind"] for line in log["lines"]] == ["command", "message"]
            assert log["lines"][0]["text"].startswith("$ python3 solo2611.py status")
            listing = (await client.get("/api/autopilot")).json()
            assert listing["active"] == 0 and listing["jobs"][0]["stage_label"] == "失败"
            # 平台自己不代写任何交付内容
            assert not (root / "delivery").exists()
    finally:
        await ctx.shutdown()


@pytest.mark.asyncio
async def test_autopilot_acks_as_autopilot_and_exports(
    fixture_factory: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root = await _app(tmp_path, monkeypatch, fixture_factory, "exported")
    state_path = root / "monitor" / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["status"] = "recorded"
    state.pop("export")
    state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    (root / "monitor" / "review-ack.json").unlink()
    (root / "delivery" / "交付表.xlsx").unlink()
    ctx = app.state.context
    await ctx.startup()
    ctx.audits.settings.mode = "off"
    pilot = ctx.autopilot
    pilot.agent_command = [sys.executable, "-c", "raise SystemExit(1)"]
    try:
        task_id = ctx.repo.get_task_by_root(root)["id"]
        await pilot.enroll([task_id])
        await pilot.tick()
        job = pilot.jobs[task_id]
        assert job.stage == "done", job.detail
        assert not pilot.agents and job.attempts == {}
        ack = json.loads((root / "monitor" / "review-ack.json").read_text(encoding="utf-8"))
        assert ack["acked"] is True and ack["ackedBy"] == "autopilot"
        reviewed = [event for event in ctx.repo.events(task_id=task_id) if event["action"] == "feedback.reviewed"]
        assert reviewed and reviewed[0]["actor"] == "autopilot" and "未经人工复核" in reviewed[0]["message"]
        assert (root / "delivery" / "交付表.xlsx").is_file()
        await pilot.cancel()
        assert pilot.jobs[task_id].stage == "done"
    finally:
        await ctx.shutdown()


def test_prompts_keep_the_agent_inside_the_gates(tmp_path: Path) -> None:
    for stage, banned in (("authoring", "不执行 run"), ("delivering", "不执行 run、reset、export")):
        text = build_prompt(stage, root=tmp_path, skill_root=tmp_path / "skill", missing=["9. 运行录屏：未录制"])
        assert banned in text and "绕过门禁的参数" in text and "AUTOPILOT: OK" in text
        assert "9. 运行录屏：未录制" in text
