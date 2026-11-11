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
from solo_auto.dispatch.gates import evaluate_container_gates
from solo_auto.dispatch.holds import HoldRegistry
from solo_auto.dispatch.policy import RecoveryPolicy
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
            explained = await client.get("/api/dispatch/explain", params={"task_id": task_id})
            assert explained.status_code == 200
            assert any(not check["ok"] for check in explained.json()["decision"]["checks"])
            pinned = await client.post(f"/api/dispatch/queue/{task_id}/pin")
            assert pinned.status_code == 200
            held = await client.post("/api/dispatch/holds/llm_guard/probe")
            assert held.status_code == 200
    finally:
        await context.shutdown()
