from __future__ import annotations

import json
from pathlib import Path

from solo_auto.pipeline.task_service import _write_workbench_marker


def test_workbench_marker_exposes_engineering_scope(tmp_path: Path) -> None:
    task_root = tmp_path / "task"
    _write_workbench_marker(
        task_root,
        task_id="task-1",
        meta={
            "projectId": "project-a",
            "projectName": "项目 A",
            "engineeringId": "eng-1",
            "name": "GB-544",
        },
        host="0.0.0.0",
        port=8099,
    )

    marker = json.loads((task_root / "monitor" / "workbench.json").read_text(encoding="utf-8"))
    assert marker == {
        "schema": "solo-auto/workbench-task/1",
        "taskId": "task-1",
        "taskRoot": str(task_root),
        "projectId": "project-a",
        "projectName": "项目 A",
        "engineeringId": "eng-1",
        "engineeringName": "GB-544",
        "apiBase": "http://127.0.0.1:8099",
    }
