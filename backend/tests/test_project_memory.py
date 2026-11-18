from __future__ import annotations

from pathlib import Path

import pytest

from solo_auto.pipeline.project_memory import ProjectMemory
from solo_auto.pipeline.prompt_templates import PromptTemplates
from solo_auto.pipeline.reconcile import Reconciler
from solo_auto.settings import app_paths
from solo_auto.store.repo import Repository


@pytest.mark.asyncio
async def test_project_memory_builds_host_only_context_pack(fixture_factory, tmp_path: Path) -> None:
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
    repo.execute(
        "UPDATE tasks SET project_id=?, engineering_id=? WHERE id=?",
        ("project-a", "engineering-a", task["id"]),
    )

    memory = ProjectMemory(repo, paths.home / "project-memory")
    snapshot = memory.snapshot_for_task(str(task["id"]), root)
    engineering = paths.home / "project-memory" / "project-a" / "engineering" / "engineering-a"
    assert Path(snapshot["pack"]) == root / "monitor" / "context-pack.md"
    assert (engineering / "analysis.md").is_file()
    assert (engineering / "repo-map.md").is_file()
    assert (engineering / "test-playbook.md").is_file()
    assert (engineering / "risks.json").is_file()
    assert (engineering / "prompts.jsonl").is_file()
    assert "工程分析" in Path(snapshot["pack"]).read_text(encoding="utf-8")

    # Claude Code only receives workspace and the formal prompt, never memory files.
    assert not any((root / "workspace").rglob("context-pack.md"))
    assert "项目记忆" not in (root / "delivery" / "提示词.md").read_text(encoding="utf-8")
    notes = memory.notes(str(task["id"]))
    assert any("安全边界" in item for item in notes)
    assert any("Claude Code" in item for item in notes)
    repo.close()


def test_prompt_template_injects_memory_only_for_orchestrator(fixture_factory, tmp_path: Path) -> None:
    paths = app_paths(tmp_path / "app")
    templates = PromptTemplates(paths)
    text = templates.render(
        "chatgpt",
        root=tmp_path / "task",
        skill_root=tmp_path / "skill",
        missing=[],
        memory=["- 项目记忆：/private/project-memory/analysis.md"],
    )
    assert "/private/project-memory/analysis.md" in text
    assert "项目记忆" in text


def test_prompt_template_requires_notes_placeholder(tmp_path: Path) -> None:
    templates = PromptTemplates(app_paths(tmp_path / "app"))
    assert templates._errors("自定义模板\nAUTOPILOT: OK\n")
    assert not templates._errors("自定义模板\n{notes}\nAUTOPILOT: OK\n")
