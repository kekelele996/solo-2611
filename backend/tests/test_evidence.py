from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from solo_auto.events.bus import EventBus
from solo_auto.evidence.diff import diff_summary, file_diff
from solo_auto.evidence.runtime import run_verification
from solo_auto.evidence.trace import step_body, timeline, verify
from solo_auto.store.repo import Repository


def test_trace_timeline_and_structure(fixture_factory) -> None:
    root = fixture_factory("exported")
    trace = root / "traces" / "-workspace" / "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa.jsonl"
    data = timeline(trace)
    assert data["counts"] == {"total": 1, "failed": 0}
    assert data["steps"][0]["tool"] == "Bash"
    assert step_body(trace, 1)["input"]["command"] == "pytest -q"
    structure = verify(trace, root / "delivery" / "提示词.md")
    assert structure["ok"] is True
    assert structure["user_messages"] == 1
    assert structure["stop_reason"] == "end_turn"


@pytest.mark.skipif(shutil.which("git") is None, reason="git 不在测试环境中")
@pytest.mark.asyncio
async def test_git_diff_summary(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(workspace)], check=True)
    subprocess.run(["git", "-C", str(workspace), "config", "user.name", "test"], check=True)
    subprocess.run(["git", "-C", str(workspace), "config", "user.email", "test@example.com"], check=True)
    file = workspace / "app.py"
    file.write_text("print('one')\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(workspace), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(workspace), "commit", "-qm", "initial"], check=True)
    initial = subprocess.check_output(["git", "-C", str(workspace), "rev-parse", "HEAD"], text=True).strip()
    file.write_text("print('one')\nprint('two')\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(workspace), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(workspace), "commit", "-qm", "artifact"], check=True)
    artifact = subprocess.check_output(["git", "-C", str(workspace), "rev-parse", "HEAD"], text=True).strip()
    summary = await diff_summary(workspace, initial, artifact)
    assert summary["total"] == {"add": 1, "del": 0, "files": 1}
    patch = await file_diff(workspace, initial, artifact, "app.py")
    assert "print('two')" in patch


@pytest.mark.asyncio
async def test_runtime_verification_records_raw_exit(tmp_path: Path) -> None:
    root = tmp_path / "task"
    workspace = root / "workspace"
    workspace.mkdir(parents=True)
    repo = Repository(tmp_path / "index.db")
    result = await run_verification(
        task_id="task-id",
        task_root=root,
        command="printf verified; printf problem >&2; exit 3",
        repo=repo,
        bus=EventBus(),
    )
    assert result["exitCode"] == 3
    assert result["stdout"] == "verified"
    assert result["stderr"] == "problem"
    assert (root / "monitor" / "runtime-verification.json").is_file()
    repo.close()
