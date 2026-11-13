from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from solo_auto.app import create_app
from solo_auto.dispatch.classify import Evidence, FailureClassifier
from solo_auto.dispatch.facts import DockerFacts, FactsSnapshot
from solo_auto.dispatch.gates import evaluate_container_gates, evaluate_session_gates
from solo_auto.dispatch.holds import HoldRegistry
from solo_auto.dispatch.policy import RecoveryPolicy
from solo_auto.dispatch.pools import PoolBook
from solo_auto.dispatch.queue import DispatchItem, DispatchQueue
from solo_auto.gateway.skill import SkillGateway
from solo_auto.settings import DispatchSettings, app_paths, load_settings, save_settings
from solo_auto.store.repo import Repository


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
    assert settings.dispatch.authoring == 3
    assert settings.dispatch.delivering == 2
    assert settings.dispatch.sessions == 5
    assert settings.dispatch.in_hand == 7
    assert settings.dispatch.disk.floor_gb == 42
    assert settings._dispatch_migrated is True
    save_settings(paths, settings)
    reloaded = load_settings(paths)
    assert reloaded.dispatch.containers == 5
    assert reloaded.dispatch.in_hand == 7
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
    assert 48 <= decision.delay_seconds <= 72


def test_launch_lost_is_retried_before_quarantine(tmp_path: Path) -> None:
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
    assert failure.kind == "agent.launch_lost"
    assert first.status == "backoff"
    assert second.status == "quarantined"


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
    assert blocked_decision.action == "quarantine"
    assert blocked_decision.status == "quarantined"

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
        return "0.3.0"

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
            # 只有失败待处理的任务可以忽略；忽略后进入已结束且保留失败原因。
            early = await client.post(f"/api/dispatch/queue/{task_id}/ignore")
            assert early.status_code == 409
            context.dispatcher.queue.update(
                task_id, status="quarantined", wait_code="quarantined", wait_text="导出被拒绝"
            )
            ignored = await client.post(f"/api/dispatch/queue/{task_id}/ignore")
            assert ignored.status_code == 200
            after = ignored.json()["snapshot"]
            assert after["counts"]["needs_action"] == 0
            assert after["counts"]["finished"] == 1
            assert after["queue"][0]["status"] == "removed"
            assert after["queue"][0]["wait_code"] == "ignored"
            assert after["queue"][0]["wait_text"] == "导出被拒绝"
            assert after["guard"]["count"] == 0
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
        assert kernel.queue.get("failed").status == "quarantined"
        assert kernel.in_hand_count() == 1
        assert f"在手 1/{kernel.effective_in_hand}" in kernel.status_sentence()
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
async def test_session_limit_follows_containers_and_counts_untracked_sessions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = await _v3_context(tmp_path, monkeypatch)
    try:
        kernel = context.dispatcher
        kernel.config.preset = "custom"
        kernel.config.containers = 3
        kernel.config.sessions = 0
        kernel.config.in_hand = 0
        context.autopilot.settings.agent_mode = "chatgpt"
        assert kernel.effective_session_limit == 3
        assert kernel.effective_in_hand == 4

        monkeypatch.setattr(type(context.autopilot), "orphan_session_keys", property(lambda _self: {"orphan:x", "orphan:y"}))
        kernel._configure_pools()
        kernel._sync_runtime_pools()
        sessions = kernel.pools_book.snapshot()["sessions"]
        assert sessions["used"] == 2 and sessions["limit"] == 3
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
        config.write_text(edited.replace("preset = \"balanced\"", "preset = \"custom\""), encoding="utf-8")
        result = await watcher.check()
        assert result["applied"] == ["dispatch", "autopilot"]
        assert context.dispatcher.effective_container_limit == 2
        assert context.dispatcher.effective_session_limit == 2
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
