from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from solo_auto.app import create_app
from solo_auto.gateway.skill import SkillGateway
from solo_auto.pipeline.autopilot import Autopilot, Job, _meaningful_progress_at, build_prompt
from solo_auto.pipeline.desktop_chatgpt import RolloutIndex

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
    app.state.context.settings.autopilot.agent_mode = "cli"
    app.state.context.autopilot.apply_settings(app.state.context.settings.autopilot)

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


@pytest.mark.asyncio
async def test_resume_restarts_failed_job_without_duplicating_active_work(
    fixture_factory: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root = await _app(tmp_path, monkeypatch, fixture_factory, "prompt-only")
    ctx = app.state.context
    await ctx.startup()
    pilot = ctx.autopilot
    pilot.agent_command = [sys.executable]
    task_id = ctx.repo.get_task_by_root(root)["id"]
    pilot.paused = True
    pilot.jobs[task_id] = Job(
        task_id=task_id,
        task_name="fixture-prompt-only",
        stage="failed",
        failure_kind="agent_reported",
        detail="CodexQueuePush 缺少辅助功能权限",
    )
    try:
        first = await pilot.resume([task_id])
        assert first["recovered"] == [task_id]
        assert pilot.paused is False
        assert pilot.jobs[task_id].stage == "pending"
        assert pilot.jobs[task_id].failure_kind == ""

        second = await pilot.resume([task_id])
        assert second["already_active"] == [task_id]
        assert second["recovered"] == []
        assert pilot.jobs[task_id].stage == "pending"
    finally:
        await ctx.shutdown()


@pytest.mark.asyncio
async def test_global_stop_freezes_plan_and_resume_relaunches_stopped_job(
    fixture_factory: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root = await _app(tmp_path, monkeypatch, fixture_factory, "prompt-only")
    ctx = app.state.context
    await ctx.startup()
    pilot = ctx.autopilot
    pilot.agent_command = [sys.executable]
    pilot.settings.stop_kills_containers = False
    task_id = ctx.repo.get_task_by_root(root)["id"]
    pilot.jobs[task_id] = Job(task_id=task_id, task_name="fixture-prompt-only", stage="authoring")
    ctx.intake.plan = {
        "id": "plan-stop-resume",
        "project_id": "project-1",
        "project_name": "项目一",
        "target": 5,
        "status": "running",
        "task_ids": [task_id],
        "attempted_engineering_ids": [],
        "claimed_engineering_ids": [],
        "failed_claims": [],
        "claim_limit": 10,
        "filters": {},
    }

    async def source_ok() -> dict[str, Any]:
        return {"ok": True, "message": "工程台连接正常"}

    monkeypatch.setattr(ctx.source, "test", source_ok)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            stopped = await client.post("/api/autopilot/stop", json={"task_ids": [], "kill_containers": False})
            assert stopped.status_code == 200
            assert stopped.json()["stopped"] == [task_id]
            assert ctx.intake.plan["status"] == "paused"
            assert "全部停止" in ctx.intake.plan["pause_reason"]

            resumed = await client.post("/api/autopilot/resume")
            assert resumed.status_code == 200
            assert resumed.json()["recovered"] == [task_id]
            assert ctx.intake.plan["status"] == "running"
            assert "pause_reason" not in ctx.intake.plan
            assert pilot.jobs[task_id].stage == "pending"
    finally:
        await ctx.shutdown()


@pytest.mark.asyncio
async def test_chatgpt_desktop_mode_respects_shared_parallel_limit(
    fixture_factory: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root = await _app(tmp_path, monkeypatch, fixture_factory, "prompt-only")
    ctx = app.state.context
    await ctx.startup()
    pilot = ctx.autopilot
    task = ctx.repo.get_task_by_root(root)
    current = Job(task_id=task["id"], task_name=task["task_name"])
    pilot.jobs = {
        "running-1": Job(task_id="running-1", task_name="运行一", desktop_running=True),
        "running-2": Job(task_id="running-2", task_name="运行二", desktop_running=True),
        "running-3": Job(task_id="running-3", task_name="运行三", desktop_running=True),
        task["id"]: current,
    }
    launched = _fake_desktop(pilot, tmp_path)
    try:
        await pilot._advance_chatgpt(current, task, root)
        assert launched == []
        assert current.stage == "pending"
        assert "3/3" in current.detail

        pilot.jobs["running-3"].desktop_running = False
        await pilot._advance_chatgpt(current, task, root)
        assert launched == [task["id"]]
    finally:
        await ctx.shutdown()


@pytest.mark.asyncio
async def test_overall_timeout_ignores_recent_reasoning_activity_and_releases_container(
    fixture_factory: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root = await _app(tmp_path, monkeypatch, fixture_factory, "prompt-only")
    ctx = app.state.context
    await ctx.startup()
    pilot = ctx.autopilot
    task = ctx.repo.get_task_by_root(root)
    now = datetime.now().astimezone().replace(microsecond=0)
    job = Job(
        task_id=task["id"],
        task_name=task["task_name"],
        stage="chatgpt",
        started_at=(now - timedelta(hours=6, minutes=1)).isoformat(),
        desktop_running=True,
        desktop_stage_id=7,
        desktop_last_progress_at=now.isoformat(),
    )
    pilot.jobs[task["id"]] = job
    closed: list[str] = []
    cleanup: list[list[str]] = []

    async def fake_finish_desktop(current: Job, **_kwargs: Any) -> None:
        closed.append(current.task_id)
        current.desktop_running = False
        current.desktop_stage_id = 0

    async def fake_gateway(argv: list[str], **_kwargs: Any) -> Any:
        cleanup.append(argv)
        return SimpleNamespace(code=0, err="")

    monkeypatch.setattr(pilot, "_finish_desktop_stage", fake_finish_desktop)
    monkeypatch.setattr(ctx.gateway, "call", fake_gateway)
    try:
        assert await pilot._enforce_task_timeout(job) is True
        assert job.stage == "failed"
        assert job.failure_kind == "agent.timeout"
        assert job.ignored is True
        assert "6 小时" in job.detail and "容器已释放" in job.detail
        assert closed == [task["id"]]
        assert cleanup and cleanup[0][:2] == ["cleanup", "--task-root"]
    finally:
        await ctx.shutdown()


def test_meaningful_progress_ignores_reasoning_rollout(tmp_path: Path) -> None:
    root = tmp_path / "task"
    (root / "monitor").mkdir(parents=True)
    (root / "workspace" / "src").mkdir(parents=True)
    state = root / "monitor" / "state.json"
    state.write_text('{"status":"running"}\n', encoding="utf-8")
    source = root / "workspace" / "src" / "app.py"
    source.write_text("print('progress')\n", encoding="utf-8")
    rollout = root / "desktop-rollout.jsonl"
    rollout.write_text('{"type":"reasoning"}\n', encoding="utf-8")
    base = datetime.now().astimezone().timestamp()
    os.utime(state, (base - 1200, base - 1200))
    os.utime(source, (base - 600, base - 600))
    os.utime(rollout, (base + 600, base + 600))

    progress = _meaningful_progress_at(root)
    assert progress is not None
    assert int(progress.timestamp()) == int(base - 600)


def test_prompts_keep_the_agent_inside_the_gates(tmp_path: Path) -> None:
    for stage, banned in (("authoring", "不执行 run"), ("delivering", "不执行 run、reset、export")):
        text = build_prompt(stage, root=tmp_path, skill_root=tmp_path / "skill", missing=["9. 运行录屏：未录制"])
        assert banned in text and "绕过门禁的参数" in text and "AUTOPILOT: OK" in text
        assert "AUTOPILOT: BLOCKED" in text
        assert "9. 运行录屏：未录制" in text


# ---- live ChatGPT session accounting -------------------------------------


def _stamp(offset_seconds: float = 0) -> str:
    moment = datetime.now(tz=datetime.now().astimezone().tzinfo) + timedelta(seconds=offset_seconds)
    return moment.astimezone().isoformat()


def _write_rollout(
    sessions: Path,
    task_id: str,
    events: list[dict[str, Any]],
    *,
    suffix: str = "abc123def456",
    age_seconds: float = 0,
    in_user_message: bool = True,
) -> Path:
    today = datetime.now()
    directory = sessions / f"{today:%Y}" / f"{today:%m}" / f"{today:%d}"
    directory.mkdir(parents=True, exist_ok=True)
    marker = f"solo-auto-chatgpt:{task_id}:{suffix}"
    path = directory / f"rollout-{today:%Y-%m-%dT%H-%M-%S}-{suffix}.jsonl"
    role = "user" if in_user_message else "assistant"
    lines = [
        {"timestamp": _stamp(-age_seconds), "type": "session_meta", "payload": {"cwd": str(sessions)}},
        {
            "timestamp": _stamp(-age_seconds),
            "type": "response_item",
            "payload": {"type": "message", "role": role, "content": [{"type": "input_text", "text": f"唯一任务标识：`{marker}`"}]},
        },
        *events,
    ]
    path.write_text("".join(json.dumps(line, ensure_ascii=False) + "\n" for line in lines), encoding="utf-8")
    if age_seconds:
        moment = datetime.now().timestamp() - age_seconds
        os.utime(path, (moment, moment))
    return path


def _event(kind: str, offset: float = 0, **payload: Any) -> dict[str, Any]:
    return {"timestamp": _stamp(offset), "type": "event_msg", "payload": {"type": kind, **payload}}


def _tool(offset: float = 0) -> dict[str, Any]:
    return {
        "timestamp": _stamp(offset),
        "type": "response_item",
        "payload": {"type": "function_call_output", "call_id": "c1", "output": "still running"},
    }


def _fake_desktop(pilot: Autopilot, tmp_path: Path) -> list[str]:
    launched: list[str] = []

    def fake_launch(**kwargs: Any) -> dict[str, Any]:
        launched.append(str(kwargs["task_id"]))
        prompt = tmp_path / f"prompt-{len(launched)}.md"
        prompt.write_text(str(kwargs["prompt"]), encoding="utf-8")
        return {
            "marker": f"solo-auto-chatgpt:{kwargs['task_id']}:launched{len(launched)}",
            "prompt_path": str(prompt),
            "deep_link": "codex://threads/new?prompt=test",
            "opened_with": "test",
            "workdir": str(tmp_path),
            "started_at": _stamp(),
        }

    pilot.desktop.launch = fake_launch  # type: ignore[method-assign]
    return launched


def test_rollout_index_reads_incrementally_and_follows_turns(isolated_codex_sessions: Path) -> None:
    task_id = "11111111-2222-3333-4444-555555555555"
    path = _write_rollout(isolated_codex_sessions, task_id, [_event("task_started"), _tool(-5)])
    _write_rollout(
        isolated_codex_sessions, "99999999-2222-3333-4444-555555555555", [], suffix="mentioned", in_user_message=False
    )
    index = RolloutIndex(isolated_codex_sessions)
    index.refresh(window_seconds=900)

    info = index.update(path)
    assert info.task_id == task_id and info.marker.endswith(":abc123def456")
    assert info.turn_state == "task_started" and info.last_tool_at > 0
    # 只在助手消息里提到标识的会话不是平台拉起的会话。
    assert [item.task_id for item in index.live(quiet_seconds=900)] == [task_id]

    error = json.dumps(_event("error", message="You've hit your usage limit. Try again at 5:00 PM."))
    with path.open("a", encoding="utf-8") as handle:
        handle.write(error[:20])  # 半行：还没写完，不能被消费
    assert index.update(path).turn_state == "task_started"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(error[20:] + "\n")
    info = index.update(path)
    assert info.turn_state == "error" and "usage limit" in info.error_text
    assert index.live(quiet_seconds=900) == []

    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_event("task_started")) + "\n")
    assert index.update(path).turn_state == "task_started"
    assert len(index.live(quiet_seconds=900)) == 1


@pytest.mark.asyncio
async def test_untracked_live_sessions_hold_capacity_until_they_go_quiet(
    fixture_factory: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_codex_sessions: Path
) -> None:
    app, root = await _app(tmp_path, monkeypatch, fixture_factory, "prompt-only")
    ctx = app.state.context
    await ctx.startup()
    pilot = ctx.autopilot
    task = ctx.repo.get_task_by_root(root)
    current = Job(task_id=task["id"], task_name=task["task_name"])
    pilot.jobs = {
        "running-1": Job(task_id="running-1", task_name="运行一", desktop_running=True),
        "running-2": Job(task_id="running-2", task_name="运行二", desktop_running=True),
        task["id"]: current,
    }
    # 平台已放弃、但在 ChatGPT 里继续运行的会话。
    orphan = _write_rollout(
        isolated_codex_sessions, "aaaaaaaa-2222-3333-4444-555555555555", [_event("task_started"), _tool()]
    )
    launched = _fake_desktop(pilot, tmp_path)
    try:
        await pilot.refresh_live_sessions(force=True)
        assert pilot.occupied_sessions() == (3, 1)
        await pilot._launch_chatgpt(current, root, [])
        assert launched == []
        assert current.stage == "pending"
        assert "3/3" in current.detail and "1 个已脱离平台跟踪" in current.detail

        stale = datetime.now().timestamp() - pilot.settings.orphan_quiet_seconds - 60
        os.utime(orphan, (stale, stale))
        await pilot.refresh_live_sessions(force=True)
        await pilot._launch_chatgpt(current, root, [])
        assert launched == [task["id"]]
    finally:
        await ctx.shutdown()


@pytest.mark.asyncio
async def test_retry_adopts_the_task_session_that_is_still_running(
    fixture_factory: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_codex_sessions: Path
) -> None:
    app, root = await _app(tmp_path, monkeypatch, fixture_factory, "prompt-only")
    ctx = app.state.context
    await ctx.startup()
    pilot = ctx.autopilot
    task = ctx.repo.get_task_by_root(root)
    current = Job(task_id=task["id"], task_name=task["task_name"], attempts={"chatgpt": 1})
    pilot.jobs = {task["id"]: current}
    rollout = _write_rollout(isolated_codex_sessions, task["id"], [_event("task_started"), _tool()])
    launched = _fake_desktop(pilot, tmp_path)
    try:
        await pilot.refresh_live_sessions(force=True)
        await pilot._launch_chatgpt(current, root, [])
        assert launched == []
        assert current.desktop_running is True
        assert current.desktop_rollout == str(rollout)
        assert current.attempts == {"chatgpt": 1}
        assert pilot.occupied_sessions() == (1, 0)
        assert any(event["action"] == "autopilot.chatgpt.adopted" for event in ctx.repo.events(limit=20))
    finally:
        await pilot._stop_desktop_follower(task["id"])
        await ctx.shutdown()


@pytest.mark.asyncio
async def test_tool_activity_keeps_a_long_container_run_alive(
    fixture_factory: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_codex_sessions: Path
) -> None:
    app, root = await _app(tmp_path, monkeypatch, fixture_factory, "prompt-only")
    ctx = app.state.context
    await ctx.startup()
    pilot = ctx.autopilot
    task = ctx.repo.get_task_by_root(root)
    hour_ago = (datetime.now().astimezone() - timedelta(hours=1)).replace(microsecond=0).isoformat()
    working = _write_rollout(isolated_codex_sessions, task["id"], [_event("task_started"), _tool(-30)], suffix="working")
    job = Job(
        task_id=task["id"],
        task_name=task["task_name"],
        stage="chatgpt",
        desktop_running=True,
        desktop_marker=f"solo-auto-chatgpt:{task['id']}:working",
        desktop_rollout=str(working),
        desktop_started_at=hour_ago,
        desktop_last_progress_at=hour_ago,
    )
    pilot.jobs = {task["id"]: job}
    terminated: list[str] = []

    async def fake_terminate(current: Job, *, detail: str, kind: str) -> None:
        terminated.append(kind)
        current.desktop_running = False

    monkeypatch.setattr(pilot, "_terminate_job", fake_terminate)
    monkeypatch.setattr(pilot, "_ensure_desktop_follower", lambda _job: None)
    try:
        await pilot._advance_chatgpt(job, task, root)
        assert terminated == []
        assert job.stage == "chatgpt" and job.desktop_running is True
        assert job.desktop_last_progress_at > hour_ago

        # 只推理、不调用工具的会话仍按无进展处理。
        idle = _write_rollout(
            isolated_codex_sessions, task["id"], [_event("task_started", -3600)], suffix="idle", age_seconds=0
        )
        job.desktop_rollout = str(idle)
        job.desktop_last_progress_at = hour_ago
        os.utime(root / "monitor" / "state.json", (0, 0))
        for path in root.rglob("*"):
            if path.is_file():
                os.utime(path, (0, 0))
        await pilot._advance_chatgpt(job, task, root)
        assert terminated == ["agent.no_progress"]
    finally:
        await ctx.shutdown()


@pytest.mark.asyncio
async def test_usage_limit_stops_new_sessions_for_the_cooldown(
    fixture_factory: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_codex_sessions: Path
) -> None:
    app, root = await _app(tmp_path, monkeypatch, fixture_factory, "prompt-only")
    ctx = app.state.context
    await ctx.startup()
    pilot = ctx.autopilot
    task = ctx.repo.get_task_by_root(root)
    current = Job(task_id=task["id"], task_name=task["task_name"])
    pilot.jobs = {task["id"]: current}
    _write_rollout(
        isolated_codex_sessions,
        "bbbbbbbb-2222-3333-4444-555555555555",
        [_event("task_started"), _event("error", message="You've hit your usage limit.")],
    )
    launched = _fake_desktop(pilot, tmp_path)
    try:
        await pilot.refresh_live_sessions(force=True)
        assert pilot.usage_limited() is True
        await pilot._launch_chatgpt(current, root, [])
        assert launched == [] and "用量限制" in current.detail
        assert any(event["action"] == "autopilot.chatgpt.usage_limited" for event in ctx.repo.events(limit=20))
    finally:
        await ctx.shutdown()



@pytest.mark.asyncio
async def test_stop_and_terminate_reap_leftover_processes_before_cleanup(
    fixture_factory: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import solo_auto.pipeline.autopilot as autopilot_module

    app, root = await _app(tmp_path, monkeypatch, fixture_factory, "prompt-only")
    ctx = app.state.context
    await ctx.startup()
    pilot = ctx.autopilot
    task = ctx.repo.get_task_by_root(root)
    order: list[str] = []

    async def fake_reap(_repo: Any, task_root: Any, **_kwargs: Any) -> list[dict[str, object]]:
        order.append(f"reap:{Path(task_root).name}")
        return [{"pid": 4242, "command": "solo2611.py run", "signal": "TERM"}]

    async def fake_gateway(argv: list[str], **_kwargs: Any) -> Any:
        order.append(argv[0])
        return SimpleNamespace(code=0, err="")

    monkeypatch.setattr(autopilot_module, "reap_and_record", fake_reap)
    monkeypatch.setattr(ctx.gateway, "call", fake_gateway)
    try:
        job = Job(task_id=task["id"], task_name=task["task_name"], stage="chatgpt", desktop_running=True)
        pilot.jobs = {task["id"]: job}
        await pilot.cancel([task["id"]], kill_containers=True)
        assert order == [f"reap:{root.name}", "cleanup"]
        assert job.stage == "stopped" and "已结束 1 个遗留进程" in job.detail

        order.clear()
        job = Job(task_id=task["id"], task_name=task["task_name"], stage="chatgpt")
        pilot.jobs = {task["id"]: job}
        await pilot._terminate_job(job, detail="测试终止", kind="agent.no_progress")
        assert order == [f"reap:{root.name}", "cleanup"]
        assert "已结束 1 个遗留进程" in job.detail

        # 选择保留容器时，运行进程也一并保留。
        order.clear()
        job = Job(task_id=task["id"], task_name=task["task_name"], stage="chatgpt")
        pilot.jobs = {task["id"]: job}
        await pilot.cancel([task["id"]], kill_containers=False)
        assert order == []
    finally:
        await ctx.shutdown()
