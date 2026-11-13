from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from solo_auto.app import create_app
from solo_auto.pipeline.prompt_history import PromptHistory
from solo_auto.store.repo import Repository


def _task(
    repo: Repository,
    root: Path,
    task_id: str,
    project_id: str,
    engineering_id: str,
    text: str,
) -> None:
    (root / "delivery").mkdir(parents=True)
    (root / "delivery" / "提示词.md").write_text(text, encoding="utf-8")
    repo.execute(
        """
        INSERT INTO tasks(
          id, task_root, task_name, source_kind, engineering_id, project_id,
          domain, sub_direction, task_type, job_status, skill_status,
          created_at, updated_at, prompt_sha256
        ) VALUES(?, ?, ?, 'local_dir', ?, ?, '数据工程', '数仓与数据湖', '功能开发',
                 'exported', 'exported', ?, ?, ?)
        """,
        (
            task_id,
            str(root),
            f"任务-{task_id}",
            engineering_id,
            project_id,
            "2026-11-13T08:00:00+00:00",
            "2026-11-13T08:00:00+00:00",
            "a" * 64,
        ),
    )


def _usage(
    repo: Repository,
    project_id: str,
    engineering_id: str,
    engineering_name: str,
    task_id: str,
) -> None:
    repo.execute(
        """
        INSERT INTO usage_ledger(
          source, project_id, project_name, engineering_id, engineering_name,
          task_id, task_name, claimed_at, task_created_at, outcome, backfilled
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 'in_progress', 0)
        """,
        (
            "http://localhost:8088",
            project_id,
            "项目 A",
            engineering_id,
            engineering_name,
            task_id,
            engineering_name,
            "2026-11-13T08:00:00+00:00",
            "2026-11-13T08:00:00+00:00",
        ),
    )


def test_project_prompt_library_and_import_deduplicate(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "solo-auto.db")
    first = tmp_path / "tasks" / "first"
    other = tmp_path / "tasks" / "other"
    _task(repo, first, "first", "project-a", "eng-1", "把订单按来源时间合并，重复执行不能新增。")
    _task(repo, other, "other", "project-b", "eng-2", "另一个项目的提示词。")
    _usage(repo, "project-a", "eng-1", "GB-544", "first")
    _usage(repo, "project-a", "eng-old", "GB-501", "purged")
    history_path = tmp_path / "prompt-history.jsonl"
    history_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "project_id": "project-a",
                        "engineering_id": "eng-old",
                        "task_id": "purged",
                        "task_name": "已清理任务",
                        "sub_direction": "数据分析与数据应用",
                        "task_type": "功能开发",
                        "text": "已删除任务留下的旧提示词。",
                        "created_at": "2026-10-01T00:00:00+00:00",
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "project_id": "project-a",
                        "engineering_id": "eng-1",
                        "task_id": "first",
                        "task_name": "任务-first",
                        "text": "把订单按来源时间合并，重复执行不能新增。",
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    library = PromptHistory(repo, history_path)
    items = library.project_library("project-a")
    assert len(items) == 2
    assert {item["taskName"] for item in items} == {"任务-first", "已清理任务"}
    assert {item["engineeringName"] for item in items} == {"GB-544", "GB-501"}

    result = library.import_library(
        "project-a",
        [
            {"text": "把订单按来源时间合并，重复执行不能新增。"},
            {"text": "新导入的对账边界提示词。"},
            {},
        ],
        source_name="prompts.json",
    )
    assert result["imported"] == 1
    assert result["skipped"] == 2
    assert result["errors"] == ["第 3 条缺少提示词正文"]
    items = library.project_library("project-a")
    assert len(items) == 3
    imported = next(item for item in items if item["source"] == "import")
    assert imported["projectId"] == "project-a"
    assert imported["engineeringName"] == "未关联工程"
    assert "新导入" in imported["text"]
    repo.close()


@pytest.mark.asyncio
async def test_prompt_library_api_import_export(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOLO_AUTO_HOME", str(tmp_path / "app"))
    monkeypatch.setenv("SOLO_AUTO_TASKS_DIR", str(tmp_path / "tasks"))
    monkeypatch.setenv("SOLO_AUTO_DISABLE_KEYCHAIN", "1")
    app = create_app(tmp_path / "app", auto_start=False)
    context = app.state.context
    await context.startup()
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            imported = await client.post(
                "/api/source/prompts/import",
                json={
                    "project_id": "project-a",
                    "source_name": "prompts.json",
                    "items": [{"text": "接口导入的提示词。"}],
                },
            )
            assert imported.status_code == 200
            assert imported.json()["imported"] == 1

            listed = await client.get(
                "/api/source/prompts",
                params={"project_id": "project-a"},
            )
            assert listed.status_code == 200
            assert listed.json()["total"] == 1
            assert listed.json()["items"][0]["text"] == "接口导入的提示词。"

            exported = await client.get(
                "/api/source/prompts/export",
                params={"project_id": "project-a"},
            )
            assert exported.status_code == 200
            assert "attachment" in exported.headers["content-disposition"]
            payload = exported.json()
            assert payload["schema"] == "solo-auto/prompt-library/1"
            assert payload["project"]["id"] == "project-a"
            assert payload["items"][0]["text"] == "接口导入的提示词。"

            missing = await client.get(
                "/api/source/prompts",
                params={"project_id": ""},
            )
            assert missing.status_code == 400
            assert missing.json()["error"]["code"] == "PROJECT_REQUIRED"
    finally:
        await context.shutdown()


@pytest.mark.asyncio
async def test_prompt_dedupe_api_queries_current_engineering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOLO_AUTO_HOME", str(tmp_path / "app"))
    monkeypatch.setenv("SOLO_AUTO_TASKS_DIR", str(tmp_path / "tasks"))
    monkeypatch.setenv("SOLO_AUTO_DISABLE_KEYCHAIN", "1")
    app = create_app(tmp_path / "app", auto_start=False)
    context = app.state.context
    await context.startup()
    history_root = tmp_path / "tasks" / "history"
    current_root = tmp_path / "tasks" / "current"
    _task(
        context.repo,
        history_root,
        "history",
        "project-a",
        "eng-1",
        "把订单按来源时间合并，重复执行不能新增记录。",
    )
    _task(
        context.repo,
        current_root,
        "current",
        "project-a",
        "eng-1",
        "设计新的库存预警题面。",
    )
    _usage(context.repo, "project-a", "eng-1", "GB-544", "history")
    _usage(context.repo, "project-a", "eng-1", "GB-544", "current")
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            duplicated = await client.post(
                "/api/source/prompts/dedupe",
                json={
                    "task_root": str(current_root),
                    "text": "把订单按来源时间合并，重复执行时不能新增记录。",
                },
            )
            assert duplicated.status_code == 200
            payload = duplicated.json()
            assert payload["engineeringName"] == "GB-544"
            assert payload["status"] == "duplicate"
            assert payload["blocking"] is True
            assert payload["summary"] == {"history": 1, "duplicate": 1, "review": 0}
            assert payload["items"][0]["taskRoot"] == str(history_root)
            assert payload["items"][0]["commonFragment"]

            clear = await client.post(
                "/api/source/prompts/dedupe",
                json={
                    "project_id": "project-a",
                    "engineering_id": "eng-1",
                    "exclude_task_id": "current",
                    "text": "对账失败后按错误码拆分重试队列并保留审计记录。",
                },
            )
            assert clear.status_code == 200
            assert clear.json()["status"] == "clear"
            assert clear.json()["summary"]["history"] == 1
    finally:
        await context.shutdown()
