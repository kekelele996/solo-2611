"""Write the FastAPI schema for TypeScript generation without starting the server."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("SOLO_AUTO_SKILL_ROOT", str(ROOT / "tests" / "fixtures" / "skill"))
os.environ.setdefault("SOLO_AUTO_HOME", tempfile.mkdtemp(prefix="solo-auto-openapi-"))

from solo_auto.app import create_app  # noqa: E402


def main() -> None:
    app = create_app(auto_start=False)
    target = (
        Path(sys.argv[1]).expanduser().resolve()
        if len(sys.argv) > 1
        else Path(__file__).resolve().parents[2] / "frontend" / "openapi.json"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(app.openapi(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
