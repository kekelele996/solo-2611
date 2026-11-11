"""Crash-safe JSON state files with backup fallback."""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        backup = path.with_suffix(path.suffix + ".bak")
        try:
            value = json.loads(backup.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            if path.exists():
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                path.rename(path.with_suffix(path.suffix + f".corrupt-{stamp}"))
            return default
        return value


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = path.with_suffix(path.suffix + ".bak")
    if path.is_file():
        shutil.copy2(path, backup)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)
