"""Launch a single uvicorn worker bound to localhost only."""

from __future__ import annotations

import sys

import uvicorn

from solo_auto.instance_lock import AlreadyRunningError
from solo_auto.settings import app_paths, load_settings


def main() -> None:
    settings = load_settings(app_paths())
    try:
        uvicorn.run(
            "solo_auto.app:app",
            host=settings.host,
            port=settings.port,
            workers=1,
            reload=False,
            access_log=True,
            timeout_graceful_shutdown=5,
        )
    except AlreadyRunningError as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
