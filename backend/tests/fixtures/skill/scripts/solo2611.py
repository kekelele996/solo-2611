"""Small CLI double used only by automated tests."""

from __future__ import annotations

import json
import sys


def emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=False))


def main() -> int:
    command = sys.argv[1]
    if command == "version":
        print("0.2.2")
    elif command == "config":
        emit({"claude": {"maxContainers": 4, "image": "test-image"}, "github": {"accessTokens": ""}})
    elif command == "cleanup":
        emit({"removed": []})
    else:
        emit({"ok": True})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
