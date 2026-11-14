"""Launch fault tolerance, SOLO2 review wait, and automatic disposal of failed tasks."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from solo_auto.app import create_app
from solo_auto.errors import ApiError
from solo_auto.gateway.skill import SkillGateway
from solo_auto.pipeline import desktop_chatgpt
from solo_auto.pipeline.autopilot import Job, awaiting_platform_feedback
from solo_auto.pipeline.desktop_chatgpt import ChatGPTDesktop


async def _context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fixture_factory: Any, names: tuple[str, ...]
) -> tuple[Any, dict[str, Path]]:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    roots = {name: fixture_factory(name, tasks_dir / name) for name in names}
    monkeypatch.setenv("SOLO_AUTO_HOME", str(tmp_path / "app"))
    monkeypatch.setenv("SOLO_AUTO_TASKS_DIR", str(tasks_dir))
    monkeypatch.setenv("SOLO_AUTO_DISPATCH__ENGINE", "v3")

    async def fake_version(self: SkillGateway) -> str:
        return "0.2.3"

    monkeypatch.setattr(SkillGateway, "version", fake_version)
    app = create_app(tmp_path / "app", auto_start=False)
    ctx = app.state.context

    async def fake_preflight() -> dict[str, Any]:
        return {"ok": True, "items": [], "checked_at": "2026-11-11T00:00:00+08:00"}

    ctx.preflight.run = fake_preflight
    await ctx.startup()
    return ctx, roots


def _write(path: Path, data: dict[str, Any], *, mtime: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    os.utime(path, (mtime, mtime))


def test_awaiting_platform_feedback_follows_latest_submission_file(tmp_path: Path) -> None:
    directory = tmp_path / "monitor" / "submission"
    assert awaiting_platform_feedback(tmp_path) is False

    _write(directory / "api-result.json", {"status": "submitted", "submissionId": None}, mtime=100)
    assert awaiting_platform_feedback(tmp_path) is False

    _write(directory / "api-result.json", {"status": "submitted", "submissionId": 42}, mtime=100)
    assert awaiting_platform_feedback(tmp_path) is True

    history = {"submissionId": 42, "history": [{"status": "QC_RUNNING"}]}
    _write(directory / "qc-history.json", history, mtime=200)
    assert awaiting_platform_feedback(tmp_path) is True

    history["history"].append({"status": "PENDING_FIX"})
    _write(directory / "qc-history.json", history, mtime=300)
    assert awaiting_platform_feedback(tmp_path) is False

    # 修复后重新提交，又回到等待质检。
    _write(directory / "api-result.json", {"status": "fix_submitted", "submissionId": 42}, mtime=400)
    assert awaiting_platform_feedback(tmp_path) is True

    history["history"].append({"status": "QC_PASSED"})
    _write(directory / "qc-history.json", history, mtime=500)
    assert awaiting_platform_feedback(tmp_path) is False


@pytest.mark.asyncio
async def test_session_waiting_for_solo2_review_frees_its_slot(
    fixture_factory: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, roots = await _context(tmp_path, monkeypatch, fixture_factory, ("prompt-only",))
    pilot = ctx.autopilot
    root = roots["prompt-only"]
    task = ctx.repo.get_task_by_root(root)
    try:
        job = Job(task_id=task["id"], task_name=task["task_name"], stage="chatgpt", desktop_running=True)
        pilot.jobs = {
            "running-1": Job(task_id="running-1", task_name="运行一", desktop_running=True),
            "running-2": Job(task_id="running-2", task_name="运行二", desktop_running=True),
            task["id"]: job,
        }
        assert pilot.occupied_sessions()[0] == 3
        assert ctx.dispatcher.queue.get(task["id"]) or ctx.dispatcher.queue.enqueue(task["id"], stage="chatgpt")
        ctx.dispatcher.queue.update(task["id"], status="active")
        in_hand = ctx.dispatcher.in_hand_count()

        _write(
            root / "monitor" / "submission" / "api-result.json",
            {"status": "submitted", "submissionId": 7},
            mtime=100,
        )
        pilot._track_platform_wait(job, root)
        assert job.awaiting_platform is True
        assert pilot.occupied_sessions()[0] == 2
        assert ctx.dispatcher.in_hand_count() == in_hand - 1
        ctx.dispatcher._sync_runtime_pools()
        sessions = ctx.dispatcher.pools_book.pools["sessions"]
        assert f"task:{task['id']}" not in sessions.observed
        assert {"task:running-1", "task:running-2"} <= sessions.observed
        assert pilot.job(task["id"])["awaiting_platform"] is True

        _write(
            root / "monitor" / "submission" / "qc-history.json",
            {"submissionId": 7, "history": [{"status": "PENDING_FIX"}]},
            mtime=200,
        )
        pilot._track_platform_wait(job, root)
        assert job.awaiting_platform is False
        assert pilot.occupied_sessions()[0] == 3
    finally:
        await ctx.shutdown()


@pytest.mark.asyncio
async def test_chatgpt_launch_failure_keeps_the_attempt_and_is_retried(
    fixture_factory: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, roots = await _context(tmp_path, monkeypatch, fixture_factory, ("prompt-only",))
    pilot = ctx.autopilot
    root = roots["prompt-only"]
    task = ctx.repo.get_task_by_root(root)
    job = Job(task_id=task["id"], task_name=task["task_name"])
    pilot.jobs = {task["id"]: job}

    def broken_launch(**_kwargs: Any) -> dict[str, Any]:
        raise ApiError(500, "CHATGPT_LAUNCH_FAILED", "打开 ChatGPT 深链接失败")

    async def open_slot(_job: Job) -> tuple[bool, str]:
        ctx.dispatcher.pools_book.acquire("sessions", f"task:{_job.task_id}")
        return True, ""

    pilot.desktop.launch = broken_launch  # type: ignore[method-assign]
    pilot._session_slot = open_slot  # type: ignore[method-assign]
    try:
        await pilot._advance_chatgpt(job, task, root)
        assert job.desktop_running is False and job.desktop_stage_id == 0
        assert job.attempts.get("chatgpt", 0) == 0
        assert job.stage == "failed" and "拉起失败" in job.detail
        stages = ctx.repo.autopilot_stages(task["id"])
        assert stages and stages[0]["outcome"] == "failed"
        item = ctx.dispatcher.queue.get(task["id"])
        assert item is not None and item.status != "quarantined"
        assert (item.last_failure or {}).get("kind") == "agent.launch_lost"
        sessions = ctx.dispatcher.pools_book.pools["sessions"]
        assert f"task:{task['id']}" not in sessions.observed | set(sessions.reserved)
    finally:
        await ctx.shutdown()


def _desktop(tmp_path: Path) -> ChatGPTDesktop:
    helper = tmp_path / "CodexQueuePush"
    helper.write_text("#!/bin/sh\n", encoding="utf-8")
    helper.chmod(0o755)
    return ChatGPTDesktop(home=tmp_path / "home", helper=str(helper), sessions_dir=tmp_path / "sessions")


def test_open_link_tolerates_helper_timeout_without_reopening(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    desktop = _desktop(tmp_path)
    calls: list[list[str]] = []

    def fake_run(args: list[str], **_kwargs: Any) -> Any:
        calls.append(args)
        raise subprocess.TimeoutExpired(args, 45)

    monkeypatch.setattr(desktop_chatgpt.subprocess, "run", fake_run)
    opened_with, warning = desktop._open_link("codex://threads/new?prompt=x")
    # 助手超时时会话可能已经建立，不能再打开一次链接造出重复会话。
    assert len(calls) == 1 and opened_with == str(desktop.helper)
    assert "继续等待会话建立" in warning


def test_open_link_falls_back_and_reports_real_blockers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    desktop = _desktop(tmp_path)
    monkeypatch.setattr(desktop_chatgpt.time, "sleep", lambda _seconds: None)
    calls: list[str] = []

    def helper_cannot_start(args: list[str], **_kwargs: Any) -> Any:
        calls.append(args[0])
        if args[0] != "open":
            raise OSError("exec format error")
        return SimpleNamespace(returncode=0 if calls.count("open") > 1 else 1, stdout="", stderr="busy")

    monkeypatch.setattr(desktop_chatgpt.subprocess, "run", helper_cannot_start)
    assert desktop._open_link("codex://x") == ("open", "")
    assert calls == [str(desktop.helper), "open", "open"]

    monkeypatch.setattr(
        desktop_chatgpt.subprocess,
        "run",
        lambda *_a, **_k: SimpleNamespace(returncode=3, stdout="", stderr="no accessibility"),
    )
    with pytest.raises(ApiError) as denied:
        desktop._open_link("codex://x")
    assert denied.value.code == "CHATGPT_LAUNCH_PERMISSION"

    desktop.helper = None
    monkeypatch.setattr(
        desktop_chatgpt.subprocess,
        "run",
        lambda *_a, **_k: SimpleNamespace(returncode=1, stdout="", stderr="open failed"),
    )
    with pytest.raises(ApiError) as failed:
        desktop._open_link("codex://x")
    assert failed.value.code == "CHATGPT_LAUNCH_FAILED"


@pytest.mark.asyncio
async def test_failed_tasks_are_disposed_without_asking(
    fixture_factory: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    names = ("prompt-only", "running-stale", "exported")
    ctx, roots = await _context(tmp_path, monkeypatch, fixture_factory, names)
    ids = {name: ctx.repo.get_task_by_root(root)["id"] for name, root in roots.items()}
    queue = ctx.dispatcher.queue
    ctx.dispatcher.config.dispose.grace_seconds = 0
    try:
        for task_id in ids.values():
            queue.enqueue(task_id, stage="chatgpt")
            queue.update(task_id, status="quarantined", wait_code="quarantined", wait_text="连续失败")
        # 人工标记保留的题不动；已导出的题保留现场。
        queue.update(ids["running-stale"], keep=True)

        results = await ctx.disposer.sweep()
        assert [row["task_id"] for row in results] == [ids["prompt-only"]]
        assert not roots["prompt-only"].exists()
        assert Path(results[0]["moved_to"]).is_relative_to(ctx.paths.trash)
        assert ctx.repo.get_task(ids["prompt-only"]) is None
        closed = queue.get(ids["prompt-only"])
        assert closed is not None and closed.status == "removed" and closed.wait_code == "auto_disposed"
        assert roots["running-stale"].is_dir() and queue.get(ids["running-stale"]).status == "quarantined"
        assert roots["exported"].is_dir() and queue.get(ids["exported"]).status == "quarantined"

        # 人工隔离同样不自动清理；取消保留后才会处理。
        queue.update(ids["running-stale"], keep=False, wait_text="人工隔离")
        assert await ctx.disposer.sweep() == []
        queue.update(ids["running-stale"], wait_text="连续失败")
        assert [row["task_id"] for row in await ctx.disposer.sweep()] == [ids["running-stale"]]

        ctx.dispatcher.config.dispose.enabled = False
        queue.update(ids["exported"], status="quarantined")
        assert await ctx.disposer.sweep() == []
    finally:
        await ctx.shutdown()


@pytest.mark.asyncio
async def test_auto_dispose_stops_after_hourly_limit(
    fixture_factory: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, roots = await _context(tmp_path, monkeypatch, fixture_factory, ("prompt-only", "running-stale"))
    queue = ctx.dispatcher.queue
    ctx.dispatcher.config.dispose.grace_seconds = 0
    ctx.dispatcher.config.dispose.max_per_hour = 1
    try:
        for root in roots.values():
            task_id = ctx.repo.get_task_by_root(root)["id"]
            queue.enqueue(task_id, stage="chatgpt")
            queue.update(task_id, status="quarantined", wait_text="连续失败")
        assert len(await ctx.disposer.sweep()) == 1
        assert await ctx.disposer.sweep() == []
        assert sum(1 for root in roots.values() if root.exists()) == 1
        events = [row["action"] for row in ctx.repo.query("SELECT action FROM events")]
        assert "task.auto_dispose_throttled" in events
    finally:
        await ctx.shutdown()
