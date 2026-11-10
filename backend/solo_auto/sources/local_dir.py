"""Local directory source used for offline test data."""

from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path
from typing import Any

from solo_auto.errors import ApiError


class LocalDirectorySource:
    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()

    async def list_projects(self, *, refresh: bool = False) -> list[dict[str, Any]]:
        return [{"id": "local", "name": "本地目录", "engineering_count": 1}] if self.root.is_dir() else []

    async def list_engineering(self, project_id: str, **_: Any) -> dict[str, Any]:
        if project_id != "local":
            raise ApiError(404, "PROJECT_NOT_FOUND", "本地题源只提供 local 项目")
        item = {
            "id": "local",
            "engineering_id": "local",
            "name": self.root.name,
            "source_file_count": _count_files(self.root),
        }
        return {"items": [item], "total": 1, "page": 1, "page_size": 50}

    async def detail(self, project_id: str, engineering_id: str) -> dict[str, Any]:
        page = await self.list_engineering(project_id)
        return page["items"][0]

    async def claim(self, project_id: str, engineering_id: str) -> dict[str, Any]:
        return await self.detail(project_id, engineering_id) | {"claim_id": "local-claim"}

    async def release(self, project_id: str, engineering_id: str, claim_id: str) -> dict[str, Any]:
        return {"ok": True}

    async def download_zip(self, project_id: str, engineering_id: str, target: Path) -> str:
        target.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in self.root.rglob("*"):
                if path.is_file() and ".git" not in path.parts:
                    archive.write(path, path.relative_to(self.root))
        digest = hashlib.sha256()
        with target.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()


def _count_files(path: Path) -> int:
    return sum(1 for item in path.rglob("*") if item.is_file() and ".git" not in item.parts)
