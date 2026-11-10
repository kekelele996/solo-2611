"""Source adapter contract."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol


class SourceAdapter(Protocol):
    async def list_projects(self, *, refresh: bool = False) -> list[dict[str, Any] | dict[str, Any]]: ...

    async def list_engineering(
        self,
        project_id: str,
        *,
        q: str = "",
        language: str = "",
        min_files: int | None = None,
        max_files: int | None = None,
        tag_ids: list[str] | None = None,
        scope: str = "available",
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]: ...

    async def detail(self, project_id: str, engineering_id: str) -> dict[str, Any]: ...

    async def claim(self, project_id: str, engineering_id: str) -> dict[str, Any]: ...

    async def release(self, project_id: str, engineering_id: str, claim_id: str) -> dict[str, Any]: ...

    async def download_zip(self, project_id: str, engineering_id: str, target: Path) -> str: ...
