"""容器优先建题门禁：最大容器数、最大任务数、严格建题冷却，以及启动页卡住的兜底。"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from solo_auto.dispatch.clock import VirtualClock
from solo_auto.dispatch.facts import DockerFacts, FactsSnapshot
from solo_auto.dispatch.gates import evaluate_creation_gates
from solo_auto.dispatch.holds import HoldRegistry
from solo_auto.gateway.skill import SkillResult
from solo_auto.pipeline.autopilot import Job
from solo_auto.pipeline.scheduler import (
    STARTUP_ABORT_SECONDS,
    STARTUP_RESUME_SECONDS,
    ActiveRun,
    QueueEntry,
    RunScheduler,
)
from solo_auto.settings import DispatchSettings
from tests.test_continuous import BankSource, _continuous_plan
from tests.test_dispatch import _v3_context
from tests.test_v2 import _app

NOW = datetime(2026, 11, 14, 12, 0).astimezone()


def _facts(*containers: str, known: bool = True) -> FactsSnapshot:
    return FactsSnapshot(
        docker=DockerFacts(known=known, healthy=known, containers=frozenset(containers), observed_at=time.monotonic()),
        disk_free_gb=500,
        observed_at=time.monotonic(),
    )


def _gate(tmp_path: Path, mode: str = "containers", **overrides: Any) -> Any:
    settings = DispatchSettings(
        mode=mode, containers=3, max_tasks=4, create_cooldown_seconds=180, startup_grace_seconds=0
    )
    values: dict[str, Any] = {
        "settings": settings,
        "desired_state": "running",
        "facts": _facts(),
        "holds": HoldRegistry(tmp_path / "holds.json"),
        "startup_elapsed": 999.0,
        "in_hand": 1,
        "container_used": 1,
        "last_created_at": 0.0,
        "now": NOW,
    }
    values.update(overrides)
    return evaluate_creation_gates(**values)


def test_cooldown_is_strict_and_reports_the_exact_resume_time(tmp_path: Path) -> None:
    blocked = _gate(tmp_path, last_created_at=NOW.timestamp() - 179)
    assert not blocked.allowed
    assert blocked.code == "create.cooldown"
    assert datetime.fromisoformat(blocked.until) == (NOW + timedelta(seconds=1)).replace(microsecond=0)
    # 容器和任务都空着，冷却也不会被提前结束。
    still_blocked = _gate(tmp_path, last_created_at=NOW.timestamp() - 10, in_hand=0, container_used=0)
    assert still_blocked.code == "create.cooldown"
    assert _gate(tmp_path, last_created_at=NOW.timestamp() - 180).allowed


def test_clock_rollback_does_not_shorten_the_cooldown(tmp_path: Path) -> None:
    decision = _gate(tmp_path, last_created_at=NOW.timestamp() + 3600)
    assert decision.code == "create.cooldown"
    assert datetime.fromisoformat(decision.until) == (NOW + timedelta(seconds=180)).replace(microsecond=0)


def test_container_first_waits_when_occupancy_would_exceed_max_containers(tmp_path: Path) -> None:
    assert _gate(tmp_path, container_used=2).allowed
    full = _gate(tmp_path, container_used=3)
    assert not full.allowed and full.code == "containers.full"
    assert "3/3" in full.text


def test_container_first_ignores_max_tasks(tmp_path: Path) -> None:
    # 容器优先：在手任务数超过最大任务数也照常建题，只看容器占用。
    assert _gate(tmp_path, in_hand=9, container_used=2).allowed


def test_task_first_caps_tasks_and_ignores_container_occupancy(tmp_path: Path) -> None:
    full = _gate(tmp_path, mode="tasks", in_hand=4, container_used=0)
    assert not full.allowed and full.code == "tasks.full"
    assert "4/4" in full.text
    # 任务优先：容器占满也照常建题，多出来的题在容器门禁处排队。
    assert _gate(tmp_path, mode="tasks", in_hand=3, container_used=5).allowed


def test_unknown_docker_never_counts_as_zero_containers(tmp_path: Path) -> None:
    decision = _gate(tmp_path, facts=_facts(known=False), container_used=0)
    assert not decision.allowed and decision.code == "docker.unknown"


def test_old_dispatch_tables_upgrade_to_max_tasks() -> None:
    from solo_auto.settings import _upgrade_dispatch

    old: dict[str, Any] = {"containers": 3}
    _upgrade_dispatch(old)
    assert old["max_tasks"] == 4
    with_in_hand: dict[str, Any] = {"containers": 3, "in_hand": 6}
    _upgrade_dispatch(with_in_hand)
    assert with_in_hand["max_tasks"] == 6
    explicit: dict[str, Any] = {"containers": 3, "in_hand": 6, "max_tasks": 2}
    _upgrade_dispatch(explicit)
    assert explicit["max_tasks"] == 2
    # 旧字段（preset、sessions、in_hand、elastic……）被忽略，不让整段 [dispatch] 回退成默认值。
    settings = DispatchSettings.model_validate(
        {"containers": 4, "in_hand": 2, "preset": "custom", "elastic": {"enabled": True}, "max_tasks": 2}
    )
    assert settings.containers == 4 and settings.max_tasks == 2
    assert "preset" not in settings.model_dump() and "elastic" not in settings.model_dump()


def _write_state(root: Path, status: str, container: str = "") -> None:
    (root / "monitor").mkdir(parents=True, exist_ok=True)
    attempts = [{"index": 1, "container": container, "status": "running"}] if container else []
    (root / "monitor" / "state.json").write_text(
        json.dumps({"status": status, "attempts": attempts}), encoding="utf-8"
    )


def _task(context: Any, tmp_path: Path, task_id: str, status: str, container: str = "") -> Path:
    root = tmp_path / "tasks" / task_id
    _write_state(root, status, container)
    now = datetime.now().astimezone().isoformat()
    context.repo.upsert_task(
        {
            "id": task_id,
            "task_root": str(root),
            "task_name": task_id,
            "source_kind": "claim",
            "domain": "数据工程",
            "sub_direction": "数仓与数据湖",
            "job_status": "running",
            "created_at": now,
            "updated_at": now,
        }
    )
    context.autopilot.jobs[task_id] = Job(task_id=task_id, task_name=task_id, stage="chatgpt")
    return root


@pytest.mark.asyncio
async def test_container_demand_counts_tasks_that_still_need_a_container(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = await _v3_context(tmp_path, monkeypatch)
    try:
        kernel = context.dispatcher
        _task(context, tmp_path, "authoring", "prepared")
        _task(context, tmp_path, "ready", "prompt_ready")
        _task(context, tmp_path, "live", "running", container="solo2611-live-01-aaaa")
        _task(context, tmp_path, "finished-run", "ran")
        stale = _task(context, tmp_path, "phantom", "running", container="solo2611-gone-01-bbbb")
        old = time.time() - kernel.config.phantom_seconds - 60
        os.utime(stale / "monitor" / "state.json", (old, old))
        kernel._fact_snapshot = _facts("solo2611-live-01-aaaa")

        demand = kernel.container_demand()
        # 运行中的容器 1 个 + 尚未拿到容器的 2 题；跑完容器阶段和失联的 running 不占。
        assert demand["live"] == 1
        assert sorted(demand["waiting_task_ids"]) == ["authoring", "ready"]
        assert demand["used"] == 3
        assert kernel.container_demand(extra=1)["used"] == 4
    finally:
        await context.shutdown()


@pytest.mark.asyncio
async def test_cooldown_survives_a_dispatcher_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = await _v3_context(tmp_path, monkeypatch)
    kernel = context.dispatcher
    kernel.config.startup_grace_seconds = 0
    kernel._fact_snapshot = _facts()
    assert kernel.creation_decision(in_hand=0).allowed
    kernel.mark_task_created(engineering_id="e1")
    created_at = kernel.last_created_at
    assert kernel.creation_decision(in_hand=0).code == "create.cooldown"
    await context.shutdown()

    from solo_auto.app import create_app

    restarted = create_app(tmp_path / "app", auto_start=False).state.context
    await restarted.startup()
    try:
        assert restarted.dispatcher.last_created_at == pytest.approx(created_at)
        restarted.dispatcher.config.startup_grace_seconds = 0
        restarted.dispatcher._fact_snapshot = _facts()
        assert restarted.dispatcher.creation_decision(in_hand=0).code == "create.cooldown"
        assert "建题冷却" in restarted.dispatcher.status_sentence()
    finally:
        await restarted.shutdown()


@pytest.mark.asyncio
async def test_intake_creates_at_most_one_task_per_cooldown(
    fixture_factory: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOLO_AUTO_DISPATCH__ENGINE", "v3")
    app, _ = await _app(tmp_path, monkeypatch, fixture_factory)
    ctx = app.state.context
    ctx.settings.storage.disk_floor_gb = 0
    kernel = ctx.dispatcher
    clock = VirtualClock(wall=NOW, monotonic=10_000.0)
    kernel.clock = clock
    kernel.started_monotonic = 0.0
    kernel.config.containers = 3
    kernel.config.max_tasks = 3
    kernel.config.create_cooldown_seconds = 180
    kernel.config.mode = "tasks"
    kernel._fact_snapshot = _facts()
    monkeypatch.setattr(ctx.staging, "list", lambda: [])

    async def fake_stage(_source: Any, project_id: str, engineering_id: str, *_: Any, **__: Any) -> dict[str, Any]:
        return {"projectId": project_id, "engineeringId": engineering_id, "name": engineering_id, "claimId": "c"}

    async def fake_create(**kwargs: Any) -> dict[str, str]:
        return {"task_id": f"task-{kwargs['staging_id']}"}

    async def fake_enroll(_task_ids: list[str]) -> None:
        return None

    monkeypatch.setattr(ctx.staging, "stage", fake_stage)
    monkeypatch.setattr(ctx.tasks, "create", fake_create)
    monkeypatch.setattr(ctx.autopilot, "enroll", fake_enroll)
    source = BankSource(["e1", "e2", "e3"])
    ctx.intake.source = source
    ctx.intake.plan = _continuous_plan(daily_cap=10)
    try:
        for _ in range(5):
            await ctx.intake.tick()
        assert source.claimed == ["e1"]
        assert "建题冷却中" in ctx.intake.plan["waiting_reason"]

        clock.advance(179)
        await ctx.intake.tick()
        assert source.claimed == ["e1"]

        clock.advance(1)
        await ctx.intake.tick()
        await ctx.intake.tick()
        assert source.claimed == ["e1", "e2"]
    finally:
        await ctx.shutdown()


class _FakeGateway:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def call(self, args: list[str], **_: Any) -> SkillResult:
        self.calls.append(list(args))
        return SkillResult(code=0, duration=0.0, data={"ok": True})


def _stuck_status(minutes: float, *, supervisor_alive: bool) -> dict[str, Any]:
    started = (datetime.now().astimezone() - timedelta(minutes=minutes)).isoformat()
    return {
        "status": "prompt_delivery",
        "sessionState": "running",
        "startedAt": started,
        "supervisorAlive": supervisor_alive,
        "promptDelivery": {"status": "waiting_trace", "repairCount": 3},
    }


@pytest.mark.asyncio
async def test_startup_stuck_session_is_resumed_then_aborted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = await _v3_context(tmp_path, monkeypatch)
    try:
        gateway = _FakeGateway()
        scheduler = RunScheduler(
            paths=context.paths,
            gateway=gateway,  # type: ignore[arg-type]
            repo=context.repo,
            reconciler=context.scheduler.reconciler,
            bus=context.bus,
        )
        root = tmp_path / "tasks" / "stuck"
        run = ActiveRun(entry=QueueEntry(task_id="stuck", task_name="卡住的题", task_root=root), started_at=time.time())

        assert await scheduler._guard_startup("stuck", run, _stuck_status(5, supervisor_alive=False)) == ""
        # 已确认送达的会话不再兜底。
        confirmed = {**_stuck_status(60, supervisor_alive=True), "promptDelivery": {"status": "confirmed"}}
        assert await scheduler._guard_startup("stuck", run, confirmed) == ""
        assert gateway.calls == []

        resume_minutes = STARTUP_RESUME_SECONDS / 60 + 1
        assert await scheduler._guard_startup("stuck", run, _stuck_status(resume_minutes, supervisor_alive=False)) == "resumed"
        assert gateway.calls[-1][:1] == ["resume"]
        # 同一题只接回一次；未到终止阈值前继续等待技能自己的补发与重启。
        assert await scheduler._guard_startup("stuck", run, _stuck_status(resume_minutes, supervisor_alive=False)) == ""

        abort_minutes = STARTUP_ABORT_SECONDS / 60 + 1
        assert await scheduler._guard_startup("stuck", run, _stuck_status(abort_minutes, supervisor_alive=True)) == "aborted"
        assert gateway.calls[-1][:2] == ["abort", "--task-root"]
        assert "prompt_delivery_stuck" in gateway.calls[-1]
        events = context.repo.query("SELECT action FROM events WHERE task_id = ?", ("stuck",))
        kinds = {row["action"] for row in events}
        assert {"session.startup_resumed", "session.startup_stuck", "run.failed"} <= kinds
    finally:
        await context.shutdown()
