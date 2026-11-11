"""Claim staging, ZIP verification and safe extraction."""

from __future__ import annotations

import hashlib
import json
import shutil
import zipfile
from pathlib import Path
from typing import Any

from solo_auto.errors import ApiError
from solo_auto.settings import Paths


class StagingManager:
    def __init__(self, paths: Paths) -> None:
        self.paths = paths

    def directory(self, engineering_id: str) -> Path:
        safe = "".join(character if character.isalnum() or character in "-_" else "-" for character in engineering_id)
        return self.paths.staging / (safe or "engineering")

    async def stage(
        self,
        source: Any,
        project_id: str,
        engineering_id: str,
        detail: dict[str, Any],
        claim_id: str,
        project_name: str = "",
    ) -> dict[str, Any]:
        directory = self.directory(engineering_id)
        if directory.exists():
            shutil.rmtree(directory)
        directory.mkdir(parents=True)
        zip_path = directory / "source.zip"
        actual_sha = await source.download_zip(project_id, engineering_id, zip_path)
        expected_sha = str(detail.get("zip_sha256") or detail.get("zipSha256") or "")
        if expected_sha and actual_sha.lower() != expected_sha.lower():
            shutil.rmtree(directory, ignore_errors=True)
            raise ApiError(
                422,
                "ZIP_SHA_MISMATCH",
                f"ZIP SHA-256 不一致：服务端 {expected_sha}，本地 {actual_sha}",
            )
        source_dir = directory / "source"
        self._safe_extract(zip_path, source_dir)
        meta = {
            "engineeringId": engineering_id,
            "projectId": project_id,
            "projectName": project_name or project_id,
            "name": str(detail.get("name") or engineering_id),
            "versionId": str(detail.get("version_id") or detail.get("versionId") or detail.get("version_number") or ""),
            "claimId": claim_id,
            "zipSha256": actual_sha,
            "expectedSha256": expected_sha,
            "tags": detail.get("tags") or detail.get("tag_ids") or [],
            "languages": detail.get("languages") or detail.get("tech_stack") or [],
            "sourceFileCount": detail.get("source_file_count") or detail.get("sourceFileCount") or 0,
            "downloadedAt": _now(),
            "stagingPath": str(directory),
            "sourcePath": str(source_dir),
            "source_kind": "project_hub",
        }
        (directory / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return meta

    def list(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for meta_path in sorted(self.paths.staging.glob("*/meta.json")):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(meta, dict):
                items.append(meta)
        return items

    def get(self, staging_id: str) -> dict[str, Any]:
        directory = self.directory(staging_id)
        meta_path = directory / "meta.json"
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ApiError(404, "STAGING_NOT_FOUND", f"暂存区不存在：{staging_id}") from exc
        if not isinstance(meta, dict) or not Path(str(meta.get("sourcePath") or "")).is_dir():
            raise ApiError(422, "STAGING_BROKEN", f"暂存区源码目录缺失：{staging_id}")
        return meta

    def attach_task(self, staging_id: str, task_root: Path) -> None:
        directory = self.directory(staging_id)
        meta_path = directory / "meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["taskRoot"] = str(task_root)
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        task_meta_dir = self.paths.run / "task-meta"
        task_meta_dir.mkdir(parents=True, exist_ok=True)
        (task_meta_dir / f"{task_root.name}.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def remove(self, engineering_id: str) -> None:
        shutil.rmtree(self.directory(engineering_id), ignore_errors=True)

    def _safe_extract(self, archive: Path, destination: Path) -> None:
        destination.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive) as zipped:
            for member in zipped.infolist():
                target = (destination / member.filename).resolve()
                if not target.is_relative_to(destination.resolve()):
                    raise ApiError(422, "ZIP_SLIP", f"ZIP 包含越界路径：{member.filename}")
            zipped.extractall(destination)
        children = list(destination.iterdir())
        if len(children) == 1 and children[0].is_dir() and not any(destination.glob(".*")):
            nested = children[0]
            for child in list(nested.iterdir()):
                shutil.move(str(child), destination / child.name)
            nested.rmdir()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _now() -> str:
    from datetime import datetime

    return datetime.now().astimezone().replace(microsecond=0).isoformat()
