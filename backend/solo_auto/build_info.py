"""Stable build identifiers shared by the API and Vite build."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from solo_auto import __version__


def build_id() -> str:
    explicit = os.environ.get("SOLO_AUTO_BUILD_ID", "").strip()
    if explicit:
        return explicit
    root = Path(__file__).resolve().parents[2]
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--short=12", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
        sha = result.stdout.strip() if result.returncode == 0 else "unknown"
    except OSError:
        sha = "unknown"
    return f"{__version__}+{sha or 'unknown'}"
