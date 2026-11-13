"""v4 session migration, status mapping, and policy tests."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from solo_auto.app import create_app
from solo_auto.dispatch.classify import Failure
from solo_auto.dispatch.policy import RecoveryPolicy
from solo_auto.dispatch.queue import DispatchItem
from solo_auto.gateway.skill import COMMANDS, SkillGateway, SkillResult
from solo_auto.pipeline.steps import derive_job_status
from solo_auto.settings import DispatchRetrySettings, ExecutionSettings
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


def test_start_failure_has_independent_limit() -> None:
    policy = RecoveryPolicy(DispatchRetrySettings(max_total_attempts=20))
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
    item.attempts["run.start_failed"] = 2
    third = policy.decide(item, failure)
    assert third.action == "quarantine"


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
