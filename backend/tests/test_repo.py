from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from solo_auto.store.repo import Repository


def _record(task_id: str, session_id: str | None = None) -> dict[str, object]:
    return {
        "id": task_id,
        "task_root": f"/tmp/{task_id}",
        "task_name": task_id,
        "source_kind": "local_dir",
        "engineering_id": "",
        "project_id": "",
        "claim_id": "",
        "domain": "数据工程",
        "sub_direction": "其他",
        "task_type": "功能开发",
        "difficulty": "困难",
        "languages": "Python",
        "repro_level": "已容器化，可一键起环境",
        "job_status": "ready",
        "skill_status": "prompt_ready",
        "skill_version": "0.2.2",
        "initial_sha": "1" * 40,
        "artifact_sha": "",
        "session_id": session_id,
        "prompt_id": "",
        "prompt_sha256": "",
        "feedback_score": None,
        "review_acked_at": "",
        "exported_at": None,
        "created_at": "2026-11-11T00:00:00+00:00",
    }


def test_six_tables_and_unique_session(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "index.db")
    tables = {row["name"] for row in repo.query("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"tasks", "steps", "attempts", "artifacts", "events", "claims"} <= tables
    repo.upsert_task(_record("one", "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"))
    with pytest.raises(sqlite3.IntegrityError):
        repo.upsert_task(_record("two", "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"))
    repo.close()


def test_steps_events_and_query(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "index.db")
    task_id = repo.upsert_task(_record("alpha"))
    repo.replace_steps(
        task_id,
        [{"number": number, "state": "done" if number < 3 else "pending", "detail": ""} for number in range(1, 12)],
    )
    repo.add_event("task.created", "created", task_id=task_id)
    rows, total = repo.list_tasks(status="ready", q="alpha")
    assert total == 1
    assert rows[0]["steps"][0]["state"] == "done"
    assert repo.events(task_id=task_id)[0]["action"] == "task.created"
    repo.close()
