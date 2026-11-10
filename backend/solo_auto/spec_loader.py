"""Mandatory bridge to the skill-owned official schema and enums."""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType
from typing import Any


def load_spec(skill_root: Path) -> ModuleType:
    scripts = str(skill_root / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    try:
        from spec import (  # type: ignore[import-not-found]
            ANCHOR_PATTERN,
            DOMAINS,
            REPRO_LEVELS,
            SUBMIT_COLUMNS,
            TASK_TYPES,
            scan_banned,
        )
    except ImportError as exc:
        raise RuntimeError(f"无法导入 solo-2611 spec.py，控制台拒绝启动: {exc}") from exc
    if len(SUBMIT_COLUMNS) != 19:
        raise RuntimeError(f"官方提交表列数异常：期望 19，实际 {len(SUBMIT_COLUMNS)}")
    module = ModuleType("solo_auto_spec")
    values: dict[str, Any] = {
        "SUBMIT_COLUMNS": SUBMIT_COLUMNS,
        "DOMAINS": DOMAINS,
        "TASK_TYPES": TASK_TYPES,
        "REPRO_LEVELS": REPRO_LEVELS,
        "ANCHOR_PATTERN": ANCHOR_PATTERN,
        "scan_banned": scan_banned,
    }
    module.__dict__.update(values)
    return module
