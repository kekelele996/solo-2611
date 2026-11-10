from __future__ import annotations

from pathlib import Path

import pytest

from solo_auto.pipeline.reconcile import Reconciler
from solo_auto.settings import app_paths
from solo_auto.store.repo import Repository


@pytest.mark.asyncio
async def test_reconcile_all_fixtures(fixture_factory, tmp_path: Path) -> None:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    for name in ("prompt-only", "running-stale", "exported"):
        fixture_factory(name, tasks_dir / name)
    paths = app_paths(tmp_path / "app")
    repo = Repository(paths.database)

    async def probe(_: str) -> bool:
        return False

    reconciler = Reconciler(repo, paths, tasks_dir, container_probe=probe)
    corrected = await reconciler.run()
    rows = repo.query("SELECT task_name, job_status FROM tasks ORDER BY task_name")
    assert corrected == 0
    assert rows == [
        {"task_name": "fixture-exported", "job_status": "exported"},
        {"task_name": "fixture-prompt-only", "job_status": "authored"},
        {"task_name": "fixture-running-stale", "job_status": "blocked"},
    ]
    second = await reconciler.run()
    assert second == 0
    repo.close()


@pytest.mark.asyncio
async def test_reconcile_marks_missing_state_orphan(fixture_factory, tmp_path: Path) -> None:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    fixture_factory("prompt-only", tasks_dir / "orphan-task")
    (tasks_dir / "orphan-task" / "monitor" / "state.json").unlink()
    paths = app_paths(tmp_path / "app")
    repo = Repository(paths.database)

    async def probe(_: str) -> bool:
        return False

    await Reconciler(repo, paths, tasks_dir, container_probe=probe).run()
    row = repo.query_one("SELECT job_status FROM tasks")
    assert row == {"job_status": "orphan"}
    repo.close()


@pytest.mark.asyncio
async def test_reconcile_indexes_duplicate_sessions_without_unique_crash(fixture_factory, tmp_path: Path) -> None:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    fixture_factory("exported", tasks_dir / "duplicate-a")
    fixture_factory("exported", tasks_dir / "duplicate-b")
    paths = app_paths(tmp_path / "app")
    repo = Repository(paths.database)

    async def probe(_: str) -> bool:
        return False

    await Reconciler(repo, paths, tasks_dir, container_probe=probe).run()
    rows = repo.query("SELECT session_id FROM tasks")
    assert len(rows) == 2
    assert all(row["session_id"] is None for row in rows)
    repo.close()
