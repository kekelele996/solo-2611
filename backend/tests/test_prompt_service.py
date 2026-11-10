from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from solo_auto.pipeline.prompt_service import PromptService
from solo_auto.pipeline.reconcile import Reconciler
from solo_auto.settings import app_paths
from solo_auto.spec_loader import load_spec
from solo_auto.store.repo import Repository


@pytest.mark.asyncio
async def test_historical_prompt_falls_back_and_reopen_requires_reason(fixture_factory, tmp_path: Path) -> None:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    root = fixture_factory("exported", tasks_dir / "task")
    state_path = root / "monitor" / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["attempts"] = []
    state["status"] = "prompt_ready"
    state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    paths = app_paths(tmp_path / "app")
    repo = Repository(paths.database)

    async def probe(_: str) -> bool:
        return False

    reconciler = Reconciler(repo, paths, tasks_dir, container_probe=probe)
    await reconciler.run()
    task = repo.get_task_by_root(root)
    assert task is not None
    service = PromptService(
        paths,
        gateway=None,  # type: ignore[arg-type]
        repo=repo,
        reconciler=reconciler,
        bus=None,  # type: ignore[arg-type]
        spec=load_spec(Path(os.environ["SOLO_AUTO_SKILL_ROOT"])),
    )
    prompt = service.get(str(task["id"]))
    assert prompt["draft"].startswith("真实项目里")
    assert prompt["readonly"] is True
    assert prompt["can_reopen"] is True
    service.reopen(str(task["id"]), "修正业务背景")
    reopened = service.get(str(task["id"]))
    assert reopened["readonly"] is False
    repo.close()
