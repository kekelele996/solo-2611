from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from solo_auto.errors import ApiError
from solo_auto.events.bus import EventBus
from solo_auto.gateway.skill import SkillResult
from solo_auto.pipeline import scheduler as scheduler_module
from solo_auto.pipeline.reconcile import Reconciler
from solo_auto.pipeline.scheduler import QueueEntry, RunScheduler
from solo_auto.settings import app_paths
from solo_auto.store.repo import Repository


class FakeGateway:
    def __init__(self, gate: asyncio.Event) -> None:
        self.gate = gate
        self.cancelled: list[str] = []

    async def call(self, argv: list[str], **_: Any) -> SkillResult:
        if argv[:2] == ["config", "show"]:
            return SkillResult(
                code=0,
                duration=0,
                data={"claude": {"maxContainers": 2}},
            )
        return SkillResult(code=0, duration=0, data={"ok": True})

    async def stream(self, *_: Any, **__: Any) -> Any:
        await self.gate.wait()

        class Result:
            code = 0
            data = {"ok": True}
            out = ""
            err = ""
            duration = 0.1

        return type("Streamed", (), {"result": Result()})

    async def cancel(self, key: str) -> bool:
        self.cancelled.append(key)
        self.gate.set()
        return True


class RecoveryGateway(FakeGateway):
    def __init__(self, gate: asyncio.Event) -> None:
        super().__init__(gate)
        self.calls: list[list[str]] = []

    async def call(self, argv: list[str], **_: Any) -> SkillResult:
        self.calls.append(argv)
        if argv[:1] == ["resume"]:
            return SkillResult(code=0, duration=0, data={"ok": True, "status": "running"})
        return await super().call(argv)

    async def run_status(self, _task_root: Path, *, task_id: str | None = None) -> SkillResult:
        return SkillResult(
            code=0,
            duration=0,
            data={
                "status": "running",
                "supervisorAlive": False,
                "containerRunning": True,
                "tmuxAlive": True,
                "attempt": 2,
                "taskId": task_id,
            },
        )


def _make_task(tmp_path: Path, repo: Repository, number: int) -> tuple[str, Path]:
    root = tmp_path / "tasks" / f"task-{number}"
    state = {
        "schema": "solo-2611/1",
        "skillVersion": "0.2.2",
        "taskName": f"task-{number}",
        "taskRoot": str(root),
        "workspace": str(root / "workspace"),
        "createdAt": f"2026-11-11T0{number}:00:00+00:00",
        "createdDate": "2026-11-11",
        "status": "prompt_ready",
        "source": {"kind": "dir"},
        "initialSha": str(number) * 40,
        "fields": {"所属领域": "数据工程", "子方向": "其他"},
        "attempts": [],
        "remote": {"initialPushed": "2026-11-11T00:00:10+00:00"},
    }
    (root / "monitor").mkdir(parents=True)
    (root / "monitor" / "state.json").write_text(json.dumps(state), encoding="utf-8")
    task_id = repo.task_id_for_root(root)
    repo.upsert_task(
        {
            "id": task_id,
            "task_root": str(root),
            "task_name": f"task-{number}",
            "source_kind": "local_dir",
            "engineering_id": "",
            "project_id": "",
            "claim_id": "",
            "domain": "数据工程",
            "sub_direction": "其他",
            "task_type": "",
            "difficulty": "",
            "languages": "",
            "repro_level": "",
            "job_status": "ready",
            "skill_status": "prompt_ready",
            "skill_version": "0.2.2",
            "initial_sha": str(number) * 40,
            "artifact_sha": "",
            "session_id": None,
            "prompt_id": "",
            "prompt_sha256": "",
            "feedback_score": None,
            "review_acked_at": "",
            "exported_at": None,
            "created_at": f"2026-11-11T0{number}:00:00+00:00",
        }
    )
    return task_id, root


@pytest.mark.asyncio
async def test_queue_gate_same_as_skill_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gate = asyncio.Event()
    paths = app_paths(tmp_path / "app")
    repo = Repository(paths.database)
    ids = [_make_task(tmp_path, repo, number)[0] for number in range(1, 6)]
    reconciler = Reconciler(repo, paths, tmp_path / "tasks", container_probe=lambda _: asyncio.sleep(0, result=False))
    gateway = FakeGateway(gate)

    async def no_containers() -> set[str]:
        return set()

    async def docker_ok() -> bool:
        return True

    monkeypatch.setattr(scheduler_module, "_running_containers", no_containers)
    monkeypatch.setattr(scheduler_module, "_docker_ok", docker_ok)
    scheduler = RunScheduler(paths=paths, gateway=gateway, repo=repo, reconciler=reconciler, bus=EventBus())  # type: ignore[arg-type]
    for task_id in ids:
        await scheduler.enqueue(task_id)
    await scheduler.refresh_limit()
    await scheduler._tick()
    await scheduler._tick()
    await scheduler._tick()
    assert len(scheduler.active) == 2
    assert len(scheduler.queue) == 3
    assert scheduler.limit == 2
    gate.set()
    await asyncio.gather(*(run.task for run in scheduler.active.values() if run.task))
    assert not scheduler.active
    repo.close()


@pytest.mark.asyncio
async def test_duplicate_enqueue_is_rejected(tmp_path: Path) -> None:
    paths = app_paths(tmp_path / "app")
    repo = Repository(paths.database)
    task_id, _ = _make_task(tmp_path, repo, 1)

    async def probe(_: str) -> bool:
        return False

    gateway = FakeGateway(asyncio.Event())
    scheduler = RunScheduler(
        paths=paths,
        gateway=gateway,  # type: ignore[arg-type]
        repo=repo,
        reconciler=Reconciler(repo, paths, tmp_path / "tasks", container_probe=probe),
        bus=EventBus(),
    )
    await scheduler.enqueue(task_id)
    with pytest.raises(ApiError, match="已在队列"):
        await scheduler.enqueue(task_id)
    repo.close()


@pytest.mark.asyncio
async def test_launch_timeout_adopts_live_session_and_resumes_supervisor(tmp_path: Path) -> None:
    paths = app_paths(tmp_path / "app")
    repo = Repository(paths.database)

    async def probe(_: str) -> bool:
        return False

    gateway = RecoveryGateway(asyncio.Event())
    scheduler = RunScheduler(
        paths=paths,
        gateway=gateway,  # type: ignore[arg-type]
        repo=repo,
        reconciler=Reconciler(repo, paths, tmp_path / "tasks", container_probe=probe),
        bus=EventBus(),
    )
    recovered = await scheduler._recover_launch_result("task-1", tmp_path / "tasks" / "task-1")
    assert recovered is not None and recovered["status"] == "running"
    assert gateway.calls and gateway.calls[0][:1] == ["resume"]
    repo.close()


@pytest.mark.asyncio
async def test_auth_breaker_clears_waiting_queue(tmp_path: Path) -> None:
    paths = app_paths(tmp_path / "app")
    repo = Repository(paths.database)
    ids = [_make_task(tmp_path, repo, number)[0] for number in range(1, 3)]

    async def probe(_: str) -> bool:
        return False

    scheduler = RunScheduler(
        paths=paths,
        gateway=FakeGateway(asyncio.Event()),  # type: ignore[arg-type]
        repo=repo,
        reconciler=Reconciler(repo, paths, tmp_path / "tasks", container_probe=probe),
        bus=EventBus(),
    )
    scheduler.queue = [
        QueueEntry(task_id=ids[0], task_name="task-1", task_root=tmp_path / "task-1"),
        QueueEntry(task_id=ids[1], task_name="task-2", task_root=tmp_path / "task-2"),
    ]
    await scheduler._trip_auth_breaker(ids[0], "401 invalid token")
    assert not scheduler.queue
    assert {row["job_status"] for row in repo.query("SELECT job_status FROM tasks")} == {"blocked"}
    repo.close()
