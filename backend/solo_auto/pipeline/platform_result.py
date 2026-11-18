"""Read a task's last platform receipt without repeatedly parsing unchanged files."""
from __future__ import annotations

import json
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo


@lru_cache(maxsize=8192)
def _receipt(path: Path, identity: tuple[int, int, int, int]) -> tuple[str, float | None]:
    data = json.loads(path.read_bytes())
    if not isinstance(data, dict) or not data.get("submissionId"):
        return "", None
    detail = data.get("detail") or data.get("existing") or {}
    finished = detail.get("qc_finished_at") if isinstance(detail, dict) else None
    try:
        parsed = datetime.fromisoformat(str(finished)) if finished else None
        if parsed and parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        timestamp = parsed.timestamp() if parsed else None
    except (ValueError, OverflowError):
        timestamp = None
    return str(data.get("status") or "").lower(), timestamp


def platform_receipt(root: str) -> tuple[str, float | None]:
    path = Path(root) / "monitor/submission/api-result.json"
    try:
        stat = path.stat()
        return _receipt(path, (stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size))
    except (OSError, ValueError):
        return "", None


def platform_status(root: str) -> str:
    return platform_receipt(root)[0]
