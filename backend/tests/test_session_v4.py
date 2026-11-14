"""v4 session migration, status mapping, and policy tests."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any

import httpx
import pytest

from solo_auto.app import create_app
from solo_auto.dispatch.classify import Failure
from solo_auto.dispatch.policy import RecoveryPolicy
from solo_auto.dispatch.queue import DispatchItem
from solo_auto.events.bus import EventBus
from solo_auto.gateway.skill import COMMANDS, SkillGateway, SkillResult
from solo_auto.pipeline.reconcile import Reconciler
from solo_auto.pipeline.scheduler import ActiveRun, QueueEntry, RunScheduler
from solo_auto.pipeline.steps import derive_job_status
from solo_auto.settings import DispatchRetrySettings, ExecutionSettings, app_paths
from solo_auto.store.repo import Repository
from solo_auto.store.state import SoloState, load_state


def test_v4_migration_is_reentrant(tmp_path: Path) -> None:
    path = tmp_path / "solo-auto.db"
    repo = Repository(path)
    columns = {
        row["name"] for row in repo.query("PRAGMA table_info(attempts)")
    }
    assert {
        "session_run_status",
        "rounds",
        "continues",
        "start_failed",
        "interrupt_kind",
        "last_activity_at",
    } <= columns
    assert repo.query_one("PRAGMA user_version")["user_version"] == 4
    repo.close()

    reopened = Repository(path)
    assert reopened.query_one(
        "SELECT version FROM schema_version WHERE version=4"
    )
    assert reopened.query_one("PRAGMA user_version")["user_version"] == 4
    reopened.close()


def test_state_accepts_session_run_and_old_tasks(tmp_path: Path) -> None:
    root = tmp_path / "task"
    (root / "monitor").mkdir(parents=True)
    path = root / "monitor" / "state.json"
    path.write_text(
        json.dumps(
            {
                "schema": "solo-2611/1",
                "taskName": "old",
                "taskRoot": str(root),
                "workspace": str(root / "workspace"),
                "createdAt": "2026-11-13T00:00:00Z",
                "status": "prompt_ready",
                "attempts": [],
            }
        ),
        encoding="utf-8",
    )
    old = load_state(root)
    assert old.compatible and old.state is not None
    assert old.state.sessionRun is None
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["sessionRun"] = {
        "schema": "solo2611/session-run/1",
        "mode": "supervised",
        "attemptIndex": 1,
        "status": "running",
        "continues": {"used": 1, "max": 2, "history": []},
        "rounds": [
            {
                "no": 1,
                "kind": "rate_limited",
                "verdict": "continue",
                "continued": True,
            }
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    loaded = load_state(root)
    assert loaded.state is not None
    assert loaded.state.sessionRun is not None
    assert loaded.state.sessionRun.continues.used == 1
    assert loaded.state.sessionRun.rounds[0].continued is True


def test_partial_v4_migration_recovers(tmp_path: Path) -> None:
    path = tmp_path / "partial.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE attempts (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          task_id TEXT NOT NULL,
          seq INTEGER NOT NULL,
          mode TEXT NOT NULL,
          container TEXT,
          started_at TEXT NOT NULL,
          finished_at TEXT,
          exit_code INTEGER,
          outcome TEXT NOT NULL,
          error_kind TEXT,
          detail TEXT,
          session_run_status TEXT,
          UNIQUE (task_id, seq)
        );
        PRAGMA user_version=3;
        """
    )
    connection.commit()
    connection.close()
    repo = Repository(path)
    assert repo.query_one("PRAGMA user_version")["user_version"] == 4
    recovered = repo.query_one(
        "SELECT action FROM events WHERE action='migration.recovered'"
    )
    assert recovered is not None
    repo.close()


def test_derive_job_status_for_managed_states() -> None:
    state = SoloState.model_validate(
        {
            "schema": "solo-2611/1",
            "taskName": "t",
            "taskRoot": "/tmp/t",
            "workspace": "/tmp/t/workspace",
            "createdAt": "2026-11-13T00:00:00Z",
            "status": "running",
            "attempts": [],
        }
    )
    assert derive_job_status(state, container_live=True) == "running"
    assert derive_job_status(state, container_live=True, session_state="holding") == "blocked"
    state.status = "session_aborted"
    assert derive_job_status(state) == "aborted"


def test_start_failure_requeues_until_the_total_cap() -> None:
    policy = RecoveryPolicy(DispatchRetrySettings(max_total_attempts=4))
    item = DispatchItem(task_id="task")
    failure = Failure(
        kind="run.start_failed",
        transient=True,
        charge=False,
        detail="REPL 未就绪",
        matched_by="test",
        max_retries=2,
    )
    first = policy.decide(item, failure)
    assert first.action == "retry"
    item.attempts["run.start_failed"] = 1
    second = policy.decide(item, failure)
    assert second.action == "retry"
    # 单类失败不再有独立上限，也不会隔离；只看累计次数。
    item.attempts["run.start_failed"] = 5
    third = policy.decide(item, failure)
    assert third.action == "retry" and third.status == "backoff"
    item.total_attempts = 3
    last = policy.decide(item, failure)
    assert last.action == "give_up" and last.status == "failed"


@pytest.mark.asyncio
async def test_start_failed_status_is_classified_before_requeue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = app_paths(tmp_path / "app")
    paths.ensure()
    repo = Repository(paths.database)

    class Gateway:
        async def call(self, *_: Any, **__: Any) -> SkillResult:
            return SkillResult(
                code=0,
                duration=0,
                data={"ok": True, "supervised": True, "attempt": 1},
            )

        async def run_status(self, *_: Any, **__: Any) -> SkillResult:
            return SkillResult(
                code=0,
                duration=0,
                data={
                    "status": "start_failed",
                    "sessionState": "start_failed",
                    "error": "题面连续 4 次自动补发仍未写入 Claude Code 轨迹",
                    "attempt": 1,
                    "rounds": [],
                    "continues": {"used": 0, "max": 2},
                },
            )

    async def no_reconcile() -> int:
        return 0

    async def no_sleep(_: float) -> None:
        return None

    reconciler = Reconciler(repo, paths, tmp_path / "tasks")
    monkeypatch.setattr(reconciler, "run", no_reconcile)
    monkeypatch.setattr("solo_auto.pipeline.scheduler.asyncio.sleep", no_sleep)
    scheduler = RunScheduler(
        paths=paths,
        gateway=Gateway(),  # type: ignore[arg-type]
        repo=repo,
        reconciler=reconciler,
        bus=EventBus(),
        attempt_total=1,
    )
    classified: list[tuple[str, str]] = []

    async def record_failure(
        task_id: str,
        *,
        kind: str,
        detail: str,
        status: dict[str, Any],
    ) -> None:
        classified.append((kind, detail))

    monkeypatch.setattr(scheduler, "_record_run_failure", record_failure)
    monkeypatch.setattr(scheduler, "_write_session_shadow", lambda *_: None)
    monkeypatch.setattr(scheduler, "_sync_session_state", lambda *_: None)
    run = ActiveRun(
        entry=QueueEntry(
            task_id="task-start-failed",
            task_name="题面未送达",
            task_root=tmp_path / "task",
        ),
        started_at=0,
    )
    scheduler.active[run.entry.task_id] = run

    await scheduler._execute(run)

    assert run.phase == "failed"
    assert classified == [
        ("run.start_failed", "题面连续 4 次自动补发仍未写入 Claude Code 轨迹")
    ]
    repo.close()


def test_session_rounds_and_stats_are_written_from_skill_state(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "solo-auto.db")
    repo.execute(
        """
        INSERT INTO tasks(
          id, task_root, task_name, source_kind, domain, sub_direction,
          job_status, skill_status, created_at, updated_at, session_id
        ) VALUES('t1', '/tmp/t1', 'task', 'local', '数据工程', '数仓与数据湖',
                 'running', 'running', '2026-11-13T00:00:00Z', '2026-11-13T00:00:00Z',
                 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa')
        """
    )
    repo.replace_session_rounds(
        "t1",
        1,
        [
            {"no": 1, "kind": "rate_limited", "verdict": "continue", "continued": True},
            {"no": 2, "kind": "none", "verdict": "complete"},
        ],
    )
    assert [row["round_no"] for row in repo.session_rounds("t1")] == [1, 2]
    repo.execute(
        """
        INSERT INTO attempts(task_id, seq, mode, started_at, outcome, continues)
        VALUES('t1', 1, 'supervised', '2026-11-13T00:00:00Z', 'accepted', 1)
        """
    )
    stats = repo.session_stats()
    assert stats["accepted"] == 1
    assert stats["acceptedWithContinue"] == 1
    assert stats["continueRate"] == 1.0
    repo.close()


def test_gateway_whitelist_and_error_classification() -> None:
    assert {"run-status", "resume", "continue", "abort"} <= COMMANDS
    assert SkillGateway.classify_result(
        SkillResult(code=0, duration=0)
    ) == "ok"
    assert SkillGateway.classify_result(
        SkillResult(code=2, duration=0, data={"ok": False})
    ) == "gate"
    assert SkillGateway.classify_result(
        SkillResult(code=1, duration=0)
    ) == "business"


def test_execution_retry_is_forced_to_one() -> None:
    settings = ExecutionSettings(attempts=3, continue_max=9, start_attempts=9)
    assert settings.attempts == 1
    assert settings.continue_max == 2
    assert settings.start_attempts == 2


@pytest.mark.asyncio
async def test_managed_scheduler_handles_four_session_outcomes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = app_paths(tmp_path / "app")
    repo = Repository(paths.database)
    tasks: list[tuple[str, Path]] = []
    for number, name in enumerate(("normal", "continued", "exhausted", "manual"), 1):
        root = tmp_path / "tasks" / name
        (root / "monitor").mkdir(parents=True)
        state = {
            "schema": "solo-2611/1",
            "taskName": name,
            "taskRoot": str(root),
            "workspace": str(root / "workspace"),
            "createdAt": f"2026-11-13T0{number}:00:00Z",
            "status": "prompt_ready",
            "attempts": [],
            "remote": {"initialPushed": "yes"},
        }
        (root / "monitor" / "state.json").write_text(json.dumps(state), encoding="utf-8")
        task_id = repo.task_id_for_root(root)
        repo.upsert_task(
            {
                "id": task_id,
                "task_root": str(root),
                "task_name": name,
                "source_kind": "local_dir",
                "engineering_id": "",
                "project_id": "",
                "claim_id": "",
                "domain": "数据工程",
                "sub_direction": "数仓与数据湖",
                "task_type": "功能开发",
                "difficulty": "困难",
                "languages": "Python",
                "repro_level": "完整",
                "job_status": "ready",
                "skill_status": "prompt_ready",
                "skill_version": "0.6.0",
                "initial_sha": "a" * 40,
                "artifact_sha": "",
                "session_id": None,
                "prompt_id": "",
                "prompt_sha256": "",
                "feedback_score": None,
                "review_acked_at": "",
                "exported_at": None,
                "created_at": "2026-11-13T00:00:00Z",
            }
        )
        tasks.append((task_id, root))

    class ManagedGateway:
        async def call(self, argv: list[str], **_: Any) -> SkillResult:
            return SkillResult(
                code=0,
                duration=0.01,
                data={"ok": True, "supervised": True, "attempt": 1},
            )

        async def run_status(self, root: Path, **_: Any) -> SkillResult:
            name = root.name
            common = {
                "attempt": 1,
                "container": f"solo2611-{name}",
                "tmuxAlive": True,
                "supervisorAlive": True,
                "startedAt": "2026-11-13T00:00:00Z",
                "lastActivityAt": "2026-11-13T00:00:02Z",
                "nextActionAt": None,
                "remainingSeconds": 9000,
            }
            if name == "normal":
                return SkillResult(
                    code=0,
                    duration=0,
                    data={
                        **common,
                        "status": "accepted",
                        "sessionState": "accepted",
                        "round": 1,
                        "continues": {"used": 0, "max": 2},
                        "rounds": [{"no": 1, "kind": "none", "verdict": "complete"}],
                        "abort": None,
                    },
                )
            if name == "continued":
                return SkillResult(
                    code=0,
                    duration=0,
                    data={
                        **common,
                        "status": "accepted",
                        "sessionState": "accepted",
                        "round": 2,
                        "continues": {"used": 1, "max": 2},
                        "rounds": [
                            {"no": 1, "kind": "rate_limited", "verdict": "continue", "continued": True},
                            {"no": 2, "kind": "none", "verdict": "complete"},
                        ],
                        "abort": None,
                    },
                )
            reason = "continues_exhausted" if name == "exhausted" else "manual_abort"
            return SkillResult(
                code=0,
                duration=0,
                data={
                    **common,
                    "status": "aborted",
                    "sessionState": "aborted",
                    "round": 2,
                    "continues": {"used": 2 if name == "exhausted" else 0, "max": 2},
                    "rounds": [{"no": 1, "kind": "truncated", "verdict": "continue"}],
                    "lastKind": "truncated",
                    "abort": {"reason": reason, "detail": reason},
                },
            )

    async def fast_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("solo_auto.pipeline.scheduler.asyncio.sleep", fast_sleep)
    gateway = ManagedGateway()
    reconciler = Reconciler(repo, paths, tmp_path / "tasks")

    async def no_reconcile() -> int:
        return 0

    monkeypatch.setattr(reconciler, "run", no_reconcile)
    scheduler = RunScheduler(
        paths=paths,
        gateway=gateway,  # type: ignore[arg-type]
        repo=repo,
        reconciler=reconciler,
        bus=EventBus(),
        attempt_total=1,
    )
    runs: list[ActiveRun] = []
    for task_id, root in tasks:
        entry = QueueEntry(task_id=task_id, task_name=root.name, task_root=root)
        run = ActiveRun(entry=entry, started_at=0)
        scheduler.active[task_id] = run
        runs.append(run)
    await asyncio.gather(*(scheduler._execute(run) for run in runs))
    assert {task_id: repo.get_task(task_id)["job_status"] for task_id, _ in tasks} == {
        tasks[0][0]: "ran",
        tasks[1][0]: "ran",
        tasks[2][0]: "aborted",
        tasks[3][0]: "aborted",
    }
    continued_rows = repo.session_rounds(tasks[1][0])
    assert len(continued_rows) == 2
    assert any(paths.sessions.glob("*.json"))
    repo.close()


@pytest.mark.asyncio
async def test_legacy_print_container_is_adopted_after_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = app_paths(tmp_path / "app")
    repo = Repository(paths.database)
    root = tmp_path / "tasks" / "legacy"
    (root / "monitor").mkdir(parents=True)
    state = {
        "schema": "solo-2611/1",
        "taskName": "legacy",
        "taskRoot": str(root),
        "workspace": str(root / "workspace"),
        "createdAt": "2026-11-13T00:00:00Z",
        "status": "running",
        "attempts": [
            {
                "index": 1,
                "container": "solo2611-legacy",
                "mode": "print",
                "startedAt": "2026-11-13T00:00:00Z",
                "status": "running",
                "dir": str(root / "monitor" / "runs" / "attempt-01"),
            }
        ],
        "remote": {"initialPushed": "yes"},
    }
    (root / "monitor" / "state.json").write_text(json.dumps(state), encoding="utf-8")
    task_id = repo.task_id_for_root(root)
    repo.upsert_task(
        {
            "id": task_id,
            "task_root": str(root),
            "task_name": "legacy",
            "source_kind": "local_dir",
            "engineering_id": "",
            "project_id": "",
            "claim_id": "",
            "domain": "数据工程",
            "sub_direction": "数仓与数据湖",
            "task_type": "",
            "difficulty": "",
            "languages": "",
            "repro_level": "",
            "job_status": "running",
            "skill_status": "running",
            "skill_version": "0.5.0",
            "initial_sha": "b" * 40,
            "artifact_sha": "",
            "session_id": None,
            "prompt_id": "",
            "prompt_sha256": "",
            "feedback_score": None,
            "review_acked_at": "",
            "exported_at": None,
            "created_at": "2026-11-13T00:00:00Z",
        }
    )

    class LegacyGateway:
        async def stream(self, argv: list[str], **_: Any) -> Any:
            assert argv[:1] == ["resume"]
            return type(
                "Streamed",
                (),
                {
                    "result": SkillResult(
                        code=0,
                        duration=0.1,
                        data={"ok": True, "status": "accepted"},
                    )
                },
            )()

    async def live_containers() -> set[str]:
        return {"solo2611-legacy"}

    monkeypatch.setattr("solo_auto.pipeline.scheduler._running_containers", live_containers)
    reconciler = Reconciler(repo, paths, tmp_path / "tasks")

    async def no_reconcile() -> int:
        return 0

    monkeypatch.setattr(reconciler, "run", no_reconcile)
    scheduler = RunScheduler(
        paths=paths,
        gateway=LegacyGateway(),  # type: ignore[arg-type]
        repo=repo,
        reconciler=reconciler,
        bus=EventBus(),
    )
    await scheduler._adopt_legacy_print_runs()
    adopted = scheduler.active[task_id]
    assert adopted.attempt == 1
    assert adopted.task is not None
    await adopted.task
    assert task_id not in scheduler.active
    assert repo.events(task_id=task_id)[-1]["action"] == "run.legacy_resumed"
    repo.close()


@pytest.mark.asyncio
async def test_session_api_reads_skill_status(
    fixture_factory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    root = fixture_factory("prompt-only", tasks / "task")
    state_path = root / "monitor" / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["status"] = "running"
    state["sessionRun"] = {
        "schema": "solo2611/session-run/1",
        "mode": "supervised",
        "attemptIndex": 1,
        "container": "solo2611-test",
        "tmuxSession": "solo2611-test",
        "status": "running",
        "supervisor": {"pid": 123},
        "continues": {"used": 1, "max": 2, "history": []},
        "rounds": [
            {"no": 1, "kind": "rate_limited", "verdict": "continue", "continued": True}
        ],
    }
    state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("SOLO_AUTO_HOME", str(tmp_path / "app"))
    monkeypatch.setenv("SOLO_AUTO_TASKS_DIR", str(tasks))
    monkeypatch.setenv("SOLO_AUTO_DISABLE_KEYCHAIN", "1")
    status_data = {
        "status": "running",
        "sessionState": "running",
        "round": 2,
        "rounds": state["sessionRun"]["rounds"],
        "continues": {"used": 1, "max": 2, "history": []},
        "lastKind": "rate_limited",
        "supervisorAlive": True,
        "container": "solo2611-test",
        "tmuxAlive": True,
        "attempt": 1,
    }

    async def fake_version(_: SkillGateway) -> str:
        return "0.6.0"

    async def fake_run_status(
        _: SkillGateway,
        __: Path,
        *,
        task_id: str | None = None,
    ) -> SkillResult:
        return SkillResult(code=0, duration=0.01, data=status_data)

    async def fake_call(
        _: SkillGateway,
        argv: list[str],
        *,
        timeout: float = 300,
        task_id: str | None = None,
        cwd: Path | None = None,
    ) -> SkillResult:
        return SkillResult(code=0, duration=0.01, data={"ok": True, "sent": True})

    monkeypatch.setattr(SkillGateway, "version", fake_version)
    monkeypatch.setattr(SkillGateway, "run_status", fake_run_status)
    monkeypatch.setattr(SkillGateway, "call", fake_call)
    app = create_app(tmp_path / "app", auto_start=False)
    context = app.state.context
    await context.startup()
    task = context.repo.get_task_by_root(root)
    assert task is not None
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            detail = await client.get(f"/api/tasks/{task['id']}/run/session")
            assert detail.status_code == 200
            assert detail.json()["continues"]["used"] == 1
            continued = await client.post(
                f"/api/tasks/{task['id']}/run/continue",
                json={"note": "test"},
            )
            assert continued.status_code == 200
            assert continued.json()["ok"] is True
    finally:
        await context.shutdown()
