from __future__ import annotations

import asyncio
import time
import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from solo_auto.app import create_app
from solo_auto.dispatch.classify import Evidence, FailureClassifier
from solo_auto.dispatch.facts import DockerFacts, FactsSnapshot, _managed_container_names
from solo_auto.dispatch.gates import evaluate_container_gates, evaluate_session_gates
from solo_auto.dispatch.holds import HoldRegistry
from solo_auto.dispatch.policy import REQUEUE_DELAY_SECONDS, RecoveryPolicy
from solo_auto.dispatch.pools import PoolBook
from solo_auto.dispatch.probes import ProbeManager
from solo_auto.dispatch.queue import DispatchItem, DispatchQueue
from solo_auto.gateway.skill import SkillGateway
from solo_auto.settings import DispatchSettings, ExecutionSettings, app_paths, load_settings, save_settings
from solo_auto.store.repo import Repository


def test_docker_facts_count_legacy_prefix_without_a_label() -> None:
    names = _managed_container_names(
        "solo2611-legacy-01-abcd\t\n"
        "custom-labelled\ttrue\n"
        "project-hub-api-1\t\n"
        "custom-app\tfalse\n"
    )
    assert names == {"solo2611-legacy-01-abcd", "custom-labelled"}


def test_execution_keeps_three_hour_session_and_stale_floor() -> None:
    execution = ExecutionSettings(run_timeout_seconds=7200, stale_after_seconds=5400)
    assert execution.run_timeout_seconds == 10800
    assert execution.stale_after_seconds == 10800


def test_legacy_settings_migrate_once_and_round_trip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SOLO_AUTO_DISPATCH__ENGINE", raising=False)
    paths = app_paths(tmp_path / "app")
    paths.ensure()
    paths.config.write_text(
        """
[execution]
max_containers = 5

[autopilot]
enabled = true
parallel_authoring = 3
parallel_delivering = 2

[intake]
max_in_hand = 7

[storage]
disk_floor_gb = 42
""".strip()
        + "\n",
        encoding="utf-8",
    )
    settings = load_settings(paths)
    assert settings.dispatch.engine == "v3"
    assert settings.dispatch.containers == 5
    assert settings.dispatch.max_tasks == 7
    assert settings.dispatch.disk.floor_gb == 42
    assert settings._dispatch_migrated is True
    save_settings(paths, settings)
    reloaded = load_settings(paths)
    assert reloaded.dispatch.containers == 5
    assert reloaded.dispatch.max_tasks == 7
    saved = tomllib.loads(paths.config.read_text(encoding="utf-8"))["dispatch"]
    for stale in ("preset", "elastic", "in_hand", "sessions", "authoring", "launch_spacing_seconds"):
        assert stale not in saved
    assert reloaded._dispatch_migrated is False


def test_invalid_dispatch_config_falls_back_without_blocking_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SOLO_AUTO_DISPATCH__ENGINE", raising=False)
    paths = app_paths(tmp_path / "app")
    paths.ensure()
    paths.config.write_text(
        """
[dispatch]
containers = 99
""".strip()
        + "\n",
        encoding="utf-8",
    )
    settings = load_settings(paths)
    assert settings.dispatch.containers == 4
    assert settings._dispatch_validation_error


def test_holds_are_scoped_and_manual_pause_is_independent(tmp_path: Path) -> None:
    registry = HoldRegistry(tmp_path / "holds.json")
    registry.register(
        source="llm_guard",
        scope=["containers"],
        reason_code="gateway.unreachable",
        reason="网关断开",
        probe_seconds=30,
    )
    registry.register(
        source="disk_guard",
        scope=["containers", "intake"],
        reason_code="disk.low",
        reason="磁盘不足",
        probe_seconds=60,
    )
    assert registry.blocking(["containers"]).id in {"llm_guard", "disk_guard"}
    assert registry.blocking(["sessions"]) is None
    released = registry.release("llm_guard", actor="probe")
    assert released is not None and released.source == "llm_guard"
    assert {hold.id for hold in registry.active()} == {"disk_guard"}
    assert registry.history[-1]["released_by"] == "probe"


def test_deprecated_backpressure_hold_is_dropped_on_reload(tmp_path: Path) -> None:
    path = tmp_path / "holds.json"
    registry = HoldRegistry(path)
    registry.register(
        source="backpressure",
        scope=["intake"],
        reason_code="backpressure",
        reason="旧版积压阈值",
    )
    reloaded = HoldRegistry(path)
    assert reloaded.holds == {}


def test_every_hold_is_probed_and_heals_without_a_human(tmp_path: Path) -> None:
    import json as _json

    path = tmp_path / "holds.json"
    # 旧版本写下的“等人处理”挂起（auto=false、没有下次探测时间）。
    path.write_text(
        _json.dumps(
            {
                "holds": [
                    {"id": "breaker", "source": "breaker", "scope": ["global"], "reason_code": "gateway.auth",
                     "reason": "鉴权失败", "since": "2026-11-13T00:00:00+00:00", "auto": False},
                    {"id": "legacy_x", "source": "legacy_x", "scope": ["global"], "reason_code": "x",
                     "reason": "旧挂起", "since": "2026-11-13T00:00:00+00:00", "auto": False},
                ]
            }
        ),
        encoding="utf-8",
    )
    registry = HoldRegistry(path)
    assert all(hold.auto for hold in registry.holds.values())
    assert {hold.source for hold in registry.due()} == {"breaker", "legacy_x"}

    probes = ProbeManager(holds=registry, facts=None, settings=DispatchSettings())  # type: ignore[arg-type]
    answers = [{"ok": False, "message": "401"}, {"ok": True, "message": "ok"}]

    async def llm_probe() -> dict[str, Any]:
        return answers.pop(0)

    probes.llm_probe = llm_probe

    async def run() -> None:
        failed = await probes.probe("breaker")
        assert not failed.ok and registry.holds["breaker"].next_at
        assert registry.holds["breaker"].last_error == "401"
        healed = await probes.probe("breaker")
        assert healed.ok and "breaker" not in registry.holds
        orphan = await probes.probe("legacy_x")
        assert orphan.ok and registry.holds == {}

    asyncio.run(run())
    assert [row["released_by"] for row in registry.history] == ["probe", "probe"]


@pytest.mark.asyncio
async def test_start_only_logs_crash_loop_and_closes_legacy_quarantine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = await _v3_context(tmp_path, monkeypatch)
    try:
        kernel = context.dispatcher
        kernel.holds.register(
            source="crash_loop", scope=["global"], reason_code="crash_loop", reason="反复异常退出"
        )
        kernel.queue.enqueue("legacy")
        kernel.queue.update("legacy", status="quarantined", wait_code="quarantined", wait_text="连续失败")
        monkeypatch.setattr(kernel, "_recent_unclean_starts", lambda: 3)
        await kernel.start()
        assert "crash_loop" not in kernel.holds.holds
        item = kernel.queue.get("legacy")
        assert item is not None and item.status == "failed" and item.wait_code == "gave_up"
        events = {row["action"]: row["level"] for row in context.repo.events(limit=50)}
        assert events.get("dispatch.crash_loop") == "warn"
    finally:
        await context.shutdown()


def test_docker_unknown_never_becomes_zero_capacity(tmp_path: Path) -> None:
    settings = DispatchSettings(containers=4, elastic={"ceiling": 6})
    item = DispatchItem(task_id="task-1")
    decision = evaluate_container_gates(
        item,
        settings=settings,
        desired_state="running",
        facts=FactsSnapshot(DockerFacts(known=False, healthy=False), 100, time.monotonic()),
        holds=HoldRegistry(tmp_path / "holds.json"),
        pools={
            "containers": {
                "used": 0,
                "limit": 4,
                "available": 4,
                "draining": False,
            }
        },
        startup_elapsed=60,
        last_launch_at=0,
        limit_resync_until="",
        now=datetime.now(UTC),
    )
    assert not decision.allowed
    assert decision.code == "docker.unknown"


def test_queue_order_persistence_and_retry(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "app.db")
    queue = DispatchQueue(repo)
    try:
        first = queue.enqueue("first")
        second = queue.enqueue("second")
        queue.update(second.task_id, priority=10)
        assert [item.task_id for item in queue.ordered()] == ["second", "first"]
        reopened = DispatchQueue(repo)
        assert [item.task_id for item in reopened.ordered()] == ["second", "first"]
        reopened.update(first.task_id, status="backoff", not_before=(datetime.now(UTC) - timedelta(seconds=1)).isoformat())
        assert [item.task_id for item in reopened.due_retry()] == ["first"]
    finally:
        repo.close()


def test_observed_runtime_sessions_are_not_double_counted() -> None:
    pools = PoolBook()
    pools.configure(containers=3, sessions=5, authoring=2, delivering=1, recording=1)
    pools.observe("sessions", {"task:a", "task:b", "task:c"})
    assert pools.pools["sessions"].used == 3
    assert pools.acquire("sessions", "task:a")
    assert pools.pools["sessions"].used == 3
    assert pools.acquire("sessions", "task:d")
    assert pools.pools["sessions"].used == 4


def test_classifier_and_backoff_policy(tmp_path: Path) -> None:
    classifier = FailureClassifier(tmp_path)
    failure = classifier.classify(
        Evidence(
            stage="running",
            exit_code=1,
            skill_stderr="HTTP 429 max_parallel_requests",
        )
    )
    assert failure.kind == "gateway.rate_limited"
    assert failure.charge is False
    decision = RecoveryPolicy(DispatchSettings().retry).decide(DispatchItem("task"), failure)
    assert decision.status == "backoff"
    assert decision.delay_seconds == REQUEUE_DELAY_SECONDS


def test_failed_run_is_requeued_at_the_tail(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "app.db")
    queue = DispatchQueue(repo)
    try:
        failing = queue.enqueue("failing")
        queue.enqueue("second")
        queue.enqueue("third")
        queue.update("failing", status="active", priority=10)
        failure = FailureClassifier(tmp_path).classify(
            Evidence(stage="chatgpt", agent_verdict="AUTOPILOT: FAILED 页面加载失败，可重试")
        )
        decision, updated = RecoveryPolicy(DispatchSettings().retry).apply(queue, failing, failure)
        assert decision.action == "retry"
        assert updated is not None and updated.status == "backoff" and updated.priority == 0
        assert updated.wait_text.startswith("第 1 次失败已记录，排到队尾重跑")
        assert [item.task_id for item in queue.ordered()][-1] == "failing"
    finally:
        repo.close()


def test_launch_lost_is_requeued_until_the_total_cap(tmp_path: Path) -> None:
    classifier = FailureClassifier(tmp_path)
    failure = classifier.classify(
        Evidence(
            stage="chatgpt",
            context={"failure_kind": "agent.launch_lost"},
        )
    )
    policy = RecoveryPolicy(DispatchSettings().retry)
    first = policy.decide(DispatchItem("task"), failure)
    second = policy.decide(DispatchItem("task", total_attempts=2), failure)
    last = policy.decide(DispatchItem("task", total_attempts=7), failure)
    assert failure.kind == "agent.launch_lost"
    assert first.status == "backoff"
    assert second.status == "backoff"
    assert last.action == "give_up" and last.status == "failed"


def test_agent_blocked_is_terminal_but_reported_failure_can_retry(tmp_path: Path) -> None:
    classifier = FailureClassifier(tmp_path)
    policy = RecoveryPolicy(DispatchSettings().retry)

    blocked = classifier.classify(
        Evidence(
            stage="chatgpt",
            agent_verdict="AUTOPILOT: BLOCKED 三次尝试已用尽，需要人工换题",
        )
    )
    blocked_decision = policy.decide(DispatchItem("blocked"), blocked)
    assert blocked.kind == "agent.blocked"
    assert blocked.disposition == "manual"
    # 会话已消耗、不能原样重跑：直接放弃，由自动清理补新题，不等人工。
    assert blocked_decision.action == "give_up"
    assert blocked_decision.status == "failed"

    failed = classifier.classify(
        Evidence(
            stage="chatgpt",
            agent_verdict="AUTOPILOT: FAILED 页面加载失败，可重试",
        )
    )
    failed_decision = policy.decide(DispatchItem("failed"), failed)
    assert failed.kind == "agent.reported"
    assert failed_decision.action == "retry"
    assert failed_decision.status == "backoff"


def test_capacity_gate_names_the_full_pool(tmp_path: Path) -> None:
    decision = evaluate_session_gates(
        DispatchItem(task_id="task-1"),
        settings=DispatchSettings(),
        desired_state="running",
        holds=HoldRegistry(tmp_path / "holds.json"),
        pools={"sessions": {"name": "sessions", "used": 4, "limit": 4, "available": 0, "draining": False}},
        startup_elapsed=60,
        last_session_at=0,
        now=datetime.now(UTC),
    )
    assert decision.code == "capacity.full"
    assert decision.text == "会话已满 4/4"


@pytest.mark.asyncio
async def test_dispatch_api_queue_and_explain(
    fixture_factory: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    fixture_factory("prompt-only", tasks_dir / "prompt-only")
    monkeypatch.setenv("SOLO_AUTO_HOME", str(tmp_path / "app"))
    monkeypatch.setenv("SOLO_AUTO_TASKS_DIR", str(tasks_dir))
    monkeypatch.setenv("SOLO_AUTO_DISABLE_KEYCHAIN", "1")
    monkeypatch.setenv("SOLO_AUTO_DISPATCH__ENGINE", "v3")

    async def fake_version(self: SkillGateway) -> str:
        return "0.4.0"

    monkeypatch.setattr(SkillGateway, "version", fake_version)
    app = create_app(tmp_path / "app", auto_start=False)
    context = app.state.context
    await context.startup()
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            task_id = (await client.get("/api/tasks")).json()["items"][0]["id"]
            queued = await client.post("/api/dispatch/queue", json={"task_ids": [task_id], "front": True})
            assert queued.status_code == 200
            assert queued.json()["added"] == [task_id]
            snapshot = (await client.get("/api/dispatch")).json()
            assert snapshot["counts"]["all"] == 1
            assert snapshot["queue"][0]["task_id"] == task_id
            upcoming = snapshot["upcoming"]
            assert [item["task_id"] for item in upcoming] == [task_id]
            assert upcoming[0]["task_name"] == snapshot["queue"][0]["task_name"]
            explained = await client.get("/api/dispatch/explain", params={"task_id": task_id})
            assert explained.status_code == 200
            assert any(not check["ok"] for check in explained.json()["decision"]["checks"])
            pinned = await client.post(f"/api/dispatch/queue/{task_id}/pin")
            assert pinned.status_code == 200
            held = await client.post("/api/dispatch/holds/llm_guard/probe")
            assert held.status_code == 200
            # 失败不需要人工处理：重试、隔离、忽略、保留接口都已删除。
            for action in ("retry", "quarantine", "ignore", "keep"):
                gone = await client.post(f"/api/dispatch/queue/{task_id}/{action}")
                assert gone.status_code in {404, 405}
            limits = (await client.get("/api/dispatch")).json()["limits"]
            assert set(limits) == {"mode", "containers", "max_tasks", "create_cooldown_seconds", "recording", "skill_limit", "synced"}
            assert "preset" not in snapshot and "elastic" not in snapshot
    finally:
        await context.shutdown()


@pytest.mark.asyncio
async def test_finished_jobs_stop_counting_as_in_hand(
    fixture_factory: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from solo_auto.pipeline.autopilot import Job

    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    monkeypatch.setenv("SOLO_AUTO_HOME", str(tmp_path / "app"))
    monkeypatch.setenv("SOLO_AUTO_TASKS_DIR", str(tasks_dir))
    monkeypatch.setenv("SOLO_AUTO_DISABLE_KEYCHAIN", "1")
    monkeypatch.setenv("SOLO_AUTO_DISPATCH__ENGINE", "v3")
    app = create_app(tmp_path / "app", auto_start=False)
    context = app.state.context
    await context.startup()
    try:
        kernel = context.dispatcher
        stages = {"running": "chatgpt", "audit": "awaiting_audit", "failed": "failed"}
        for task_id, stage in stages.items():
            kernel.queue.enqueue(task_id, stage="chatgpt")
            kernel.queue.update(task_id, status="active")
            context.autopilot.jobs[task_id] = Job(task_id=task_id, task_name=task_id, stage=stage)
        assert kernel.in_hand_count() == 3
        kernel._sync_runtime_pools()
        assert kernel.queue.get("audit").status == "done"
        assert kernel.queue.get("failed").status == "failed"
        assert kernel.in_hand_count() == 1
        assert "容器优先" in kernel.status_sentence() and "任务 1 ·" in kernel.status_sentence()
        kernel.config.mode = "tasks"
        kernel.config.max_tasks = 3
        assert "任务优先" in kernel.status_sentence() and "任务 1/3" in kernel.status_sentence()
        kernel.config.mode = "containers"
        outcomes = kernel.repo.query("SELECT task_id FROM dispatch_outcomes")
        kernel._sync_runtime_pools()
        kernel.on_job_finished("audit", outcome="done")
        assert len(kernel.repo.query("SELECT task_id FROM dispatch_outcomes")) == len(outcomes) == 2
    finally:
        await context.shutdown()


async def _v3_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    monkeypatch.setenv("SOLO_AUTO_HOME", str(tmp_path / "app"))
    monkeypatch.setenv("SOLO_AUTO_TASKS_DIR", str(tasks_dir))
    monkeypatch.setenv("SOLO_AUTO_DISABLE_KEYCHAIN", "1")
    monkeypatch.setenv("SOLO_AUTO_DISPATCH__ENGINE", "v3")

    async def fake_version(self: SkillGateway) -> str:
        return "0.2.3"

    monkeypatch.setattr(SkillGateway, "version", fake_version)
    app = create_app(tmp_path / "app", auto_start=False)
    context = app.state.context
    await context.startup()
    return context


@pytest.mark.asyncio
async def test_session_limit_follows_max_tasks_and_counts_untracked_sessions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = await _v3_context(tmp_path, monkeypatch)
    try:
        kernel = context.dispatcher
        kernel.config.containers = 3
        context.autopilot.settings.agent_mode = "chatgpt"
        # 容器优先：不限在手任务数，会话按硬上限 8 个放开。
        kernel.config.mode = "containers"
        kernel.config.max_tasks = 3
        assert kernel.effective_session_limit == 8
        # 任务优先：在手和会话都跟随最大任务数，会话最多 8 个。
        kernel.config.mode = "tasks"
        kernel.config.max_tasks = 12
        assert kernel.effective_in_hand == 12
        assert kernel.effective_session_limit == 8
        kernel.config.max_tasks = 3
        assert kernel.effective_in_hand == 3
        assert kernel.effective_session_limit == 3

        monkeypatch.setattr(type(context.autopilot), "orphan_session_keys", property(lambda _self: {"orphan:x", "orphan:y"}))
        kernel._configure_pools()
        kernel._sync_runtime_pools()
        sessions = kernel.pools_book.snapshot()["sessions"]
        assert sessions["used"] == 2 and sessions["limit"] == 3
    finally:
        await context.shutdown()


@pytest.mark.asyncio
async def test_dispatch_mode_switches_through_the_limits_api(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = await _v3_context(tmp_path, monkeypatch)

    async def fake_sync(*, sync: bool = True) -> dict[str, Any]:
        return {"synced": True}

    monkeypatch.setattr(context.dispatcher, "sync_skill_limit", fake_sync)
    try:
        kernel = context.dispatcher
        assert kernel.snapshot()["limits"]["mode"] == "containers"
        result = await kernel.update_limits({"mode": "tasks", "max_tasks": 6})
        assert result["changed"]["mode"] == ["containers", "tasks"]
        assert result["snapshot"]["limits"]["mode"] == "tasks"
        assert kernel.effective_in_hand == 6 and kernel.effective_session_limit == 6
        assert kernel.creation_view()["mode"] == "tasks"
        saved = tomllib.loads(context.paths.config.read_text(encoding="utf-8"))["dispatch"]
        assert saved["mode"] == "tasks" and saved["max_tasks"] == 6
    finally:
        await context.shutdown()


@pytest.mark.asyncio
async def test_config_file_edits_apply_without_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = await _v3_context(tmp_path, monkeypatch)
    try:
        synced: list[int] = []

        async def fake_sync(*, sync: bool = True) -> dict[str, Any]:
            synced.append(context.dispatcher.effective_container_limit)
            return {"ok": True}

        monkeypatch.setattr(context.dispatcher, "sync_skill_limit", fake_sync)
        watcher = context.config_watcher
        config = context.paths.config
        text = config.read_text(encoding="utf-8")
        assert await watcher.check() == {"applied": [], "restart_required": []}

        edited = text.replace("\ncontainers = 4", "\ncontainers = 2").replace(
            "task_timeout_seconds = 21600", "task_timeout_seconds = 18000"
        )
        assert edited != text
        config.write_text(edited, encoding="utf-8")
        result = await watcher.check()
        assert result["applied"] == ["dispatch", "autopilot"]
        assert context.dispatcher.effective_container_limit == 2
        # 默认容器优先：会话上限不随容器数变化。
        assert context.settings.dispatch.mode == "containers"
        assert context.dispatcher.effective_session_limit == 8
        assert context.autopilot.settings.task_timeout_seconds == 18000
        assert context.settings.dispatch.containers == 2
        assert synced == [2]

        # 写坏的 [dispatch] 不能把调度回退成默认值。
        broken = config.read_text(encoding="utf-8").replace("\ncontainers = 2", "\ncontainers = 99")
        config.write_text(broken, encoding="utf-8")
        result = await watcher.check()
        assert result["applied"] == []
        assert context.dispatcher.effective_container_limit == 2

        config.write_text(
            broken.replace("\nport = ", "\nport = 1").replace("\ncontainers = 99", "\ncontainers = 2"), encoding="utf-8"
        )
        result = await watcher.check()
        assert result["restart_required"] == ["port"]
        actions = [event["action"] for event in context.repo.events(limit=50)]
        assert "config.reloaded" in actions and "config.reload_failed" in actions and "config.restart_required" in actions
    finally:
        await context.shutdown()
