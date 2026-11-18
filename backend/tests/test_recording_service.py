from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from solo_auto.pipeline.recording_service import RecordingService
from solo_auto.settings import RecordingSettings


def _service(root: Path) -> RecordingService:
    task = {"id": "task-1", "task_root": str(root)}
    return RecordingService(
        paths=cast(Any, SimpleNamespace()),
        settings=RecordingSettings(max_seconds=90),
        gateway=cast(Any, SimpleNamespace()),
        repo=cast(Any, SimpleNamespace(get_task=lambda task_id: task if task_id == "task-1" else None)),
        reconciler=cast(Any, SimpleNamespace()),
        bus=cast(Any, SimpleNamespace()),
    )


@pytest.mark.asyncio
async def test_recording_verify_requires_service_cleanup_evidence(tmp_path: Path) -> None:
    root = tmp_path / "task"
    (root / "monitor").mkdir(parents=True)
    (root / "recording").mkdir()
    video = root / "recording" / "运行录屏.mp4"
    video.write_bytes(b"video")
    (root / "monitor" / "state.json").write_text(
        json.dumps(
            {
                "schema": "solo-2611/1",
                "recording": {
                    "video": str(video),
                    "durationSeconds": 30,
                    "width": 1280,
                    "height": 720,
                    "windowCaptureOk": True,
                    "pointerStrategy": "none",
                },
            }
        ),
        encoding="utf-8",
    )
    (root / "monitor" / "record-plan.json").write_text(
        json.dumps({"cleanupCommands": ["docker compose down"]}),
        encoding="utf-8",
    )
    for name in ("运行录屏-window-capture.json", "运行录屏-cursor-guard.json"):
        (root / "recording" / name).write_text(
            json.dumps({"status": "ok", "pointerStrategy": "none"}),
            encoding="utf-8",
        )

    service = _service(root)
    missing = await service.verify("task-1")
    assert missing["ok"] is False
    assert missing["service_cleanup_ok"] is False
    assert "录制结束后未确认服务已停止" in missing["errors"]

    (root / "recording" / "service-cleanup.json").write_text(
        json.dumps({"status": "ok", "ok": True, "message": "录屏服务已自动停止"}),
        encoding="utf-8",
    )
    passed = await service.verify("task-1")
    assert passed["ok"] is True
    assert passed["service_cleanup_ok"] is True
