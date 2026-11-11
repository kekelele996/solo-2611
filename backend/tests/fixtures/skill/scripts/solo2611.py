"""Small CLI double used only by automated tests."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def arg(name: str) -> str:
    return sys.argv[sys.argv.index(name) + 1]


def emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=False))


def main() -> int:
    command = sys.argv[1]
    if command == "version":
        print("0.2.2")
    elif command == "config":
        emit({"claude": {"maxContainers": 4, "image": "test-image"}, "github": {"accessTokens": ""}})
    elif command == "record" and sys.argv[2] == "terminal":
        emit({"ok": True, "windowId": 4242, "title": "solo-2611 录屏 — workspace — zsh -i"})
    elif command == "export":
        root = Path(arg("--task-root"))
        xlsx = root / "delivery" / "交付表.xlsx"
        xlsx.write_bytes(b"fixture")
        state_path = root / "monitor" / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["status"] = "exported"
        state["export"] = {"xlsx": str(xlsx), "missing": [], "exportedAt": "2026-11-11T04:00:00+00:00",
                           "exportedDate": "2026-11-11"}
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        emit({"ok": True, "xlsx": str(xlsx), "missing": []})
    elif command == "export-batch":
        Path(arg("--out")).write_bytes(b"fixture")
        emit({"ok": True, "rows": 1, "skipped": []})
    elif command == "cleanup":
        emit({"removed": []})
    else:
        emit({"ok": True})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
