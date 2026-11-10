from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

import pytest

from solo_auto.events.bus import EventBus
from solo_auto.gateway.skill import SkillResult
from solo_auto.pipeline.feedback_service import FeedbackService
from solo_auto.pipeline.reconcile import Reconciler
from solo_auto.settings import app_paths
from solo_auto.spec_loader import load_spec
from solo_auto.store.repo import Repository


class FakeFeedbackGateway:
    async def call(self, argv: list[str], **_: Any) -> SkillResult:
        assert argv[0] == "feedback"
        return SkillResult(
            code=2,
            duration=0.01,
            data={"ok": False, "score": 4, "errors": ["179 字"], "warnings": []},
        )


@pytest.mark.asyncio
async def test_feedback_is_stored_exactly_and_errors_pass_through(fixture_factory, tmp_path: Path) -> None:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    root = fixture_factory("exported", tasks_dir / "task")
    paths = app_paths(tmp_path / "app")
    repo = Repository(paths.database)

    async def probe(_: str) -> bool:
        return False

    reconciler = Reconciler(repo, paths, tasks_dir, container_probe=probe)
    await reconciler.run()
    task = repo.get_task_by_root(root)
    assert task is not None
    gateway = FakeFeedbackGateway()
    service = FeedbackService(
        paths,
        gateway=gateway,  # type: ignore[arg-type]
        repo=repo,
        reconciler=reconciler,
        bus=EventBus(),
        spec=load_spec(Path(os.environ["SOLO_AUTO_SKILL_ROOT"])),
    )
    acknowledged = service.ack(str(task["id"]), acked=True)
    assert acknowledged["acked"] is True
    assert (root / "monitor" / "review-ack.json").is_file()
    service.save(str(task["id"]), "")
    feedback_path = root / "delivery" / "feedback.md"
    assert feedback_path.read_text(encoding="utf-8") == ""
    assert hashlib.sha256(feedback_path.read_bytes()).hexdigest() == hashlib.sha256(b"").hexdigest()

    feedback_path.write_text("## 交付完整性\n\n4\n", encoding="utf-8")
    checked = await service.check(str(task["id"]))
    assert checked["ok"] is False
    assert checked["errors"] == ["179 字"]
    repo.close()
