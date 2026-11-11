"""Recoverable task trash with durable manifests."""

from __future__ import annotations

import builtins
import json
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from solo_auto.errors import ApiError
from solo_auto.events.bus import EventBus
from solo_auto.settings import Paths, TrashSettings
from solo_auto.store.repo import Repository


class TrashService:
    def __init__(
        self,
        *,
        paths: Paths,
        repo: Repository,
        bus: EventBus,
        settings: TrashSettings,
    ) -> None:
        self.paths = paths
        self.repo = repo
        self.bus = bus
        self.settings = settings

    def move(
        self,
        *,
        task: dict[str, Any],
        sources: list[Path],
        release_error: str = "",
    ) -> dict[str, Any]:
        root = Path(str(task["task_root"]))
        target = self.paths.trash / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{root.name}"
        suffix = 1
        while target.exists():
            target = target.with_name(f"{target.name}-{suffix}")
            suffix += 1
        target.mkdir(parents=True, exist_ok=False)
        moved: list[dict[str, Any]] = []
        total_size = 0
        for index, source in enumerate(sources):
            if not source.exists():
                continue
            item_size = _size(source)
            destination = target / f"{index}-{source.name}"
            shutil.move(str(source), str(destination))
            total_size += item_size
            moved.append(
                {
                    "original": str(source),
                    "moved": destination.name,
                    "kind": "directory" if destination.is_dir() else "file",
                }
            )
        meta = {
            "version": 1,
            "name": target.name,
            "task_id": str(task["id"]),
            "task_name": str(task["task_name"]),
            "job_status": str(task.get("job_status") or ""),
            "cleaned_at": _now(),
            "expires_at": _expires(self.settings.retention_days),
            "size": total_size,
            "moved": moved,
            "task": task,
            "release_error": release_error,
        }
        self._write_meta(target, meta)
        self.bus.publish("trash.updated", {"name": target.name, "action": "moved"})
        return meta

    def list(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for path in sorted(self.paths.trash.iterdir(), key=lambda item: item.name, reverse=True):
            if not path.is_dir():
                continue
            meta = _read_json(path / "meta.json")
            if not meta:
                meta = {
                    "version": 0,
                    "name": path.name,
                    "task_id": "",
                    "task_name": path.name,
                    "cleaned_at": datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(),
                    "expires_at": "",
                    "size": _size(path),
                    "moved": [],
                    "legacy": True,
                }
            meta["remaining_days"] = _remaining_days(str(meta.get("expires_at") or ""))
            items.append(meta)
        return items

    def restore(self, name: str) -> dict[str, Any]:
        directory, meta = self._entry(name)
        if meta.get("legacy") or not meta.get("moved"):
            raise ApiError(409, "TRASH_LEGACY_ENTRY", "旧版回收站条目缺少恢复清单，只能手动恢复")
        moved = [item for item in meta["moved"] if isinstance(item, dict)]
        originals = [Path(str(item.get("original") or "")) for item in moved]
        occupied = [str(path) for path in originals if path.exists()]
        if occupied:
            raise ApiError(
                409,
                "TRASH_PATH_OCCUPIED",
                "原路径已被占用，回收站条目已保留",
                detail={"occupied": occupied},
            )
        restored: list[str] = []
        for item, original in zip(moved, originals, strict=True):
            source = directory / str(item.get("moved") or "")
            if not source.exists():
                continue
            original.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(original))
            restored.append(str(original))
        task = meta.get("task")
        if isinstance(task, dict) and task.get("id"):
            self.repo.upsert_task(task)
        shutil.rmtree(directory)
        self.repo.add_event(
            "trash.restored",
            f"{meta.get('task_name') or name} 已从回收站恢复",
            actor="user",
            task_id=str(meta.get("task_id") or "") or None,
            details={"name": name, "restored": restored},
        )
        self.bus.publish("trash.updated", {"name": name, "action": "restored"})
        return {"ok": True, "name": name, "restored": restored, "task_id": meta.get("task_id")}

    def delete(self, name: str) -> dict[str, Any]:
        directory, meta = self._entry(name)
        size = _size(directory)
        shutil.rmtree(directory)
        self.repo.add_event(
            "trash.deleted",
            f"回收站条目已永久删除：{meta.get('task_name') or name}",
            level="warn",
            actor="user",
            task_id=str(meta.get("task_id") or "") or None,
            details={"name": name, "size": size},
        )
        self.bus.publish("trash.updated", {"name": name, "action": "deleted"})
        return {"ok": True, "name": name, "size": size}

    def empty(self) -> dict[str, Any]:
        items = self.list()
        removed: list[str] = []
        for item in items:
            result = self.delete(str(item["name"]))
            removed.append(str(result["name"]))
        return {"ok": True, "removed": removed}

    def purge_expired(self) -> builtins.list[str]:
        if self.settings.retention_days <= 0:
            return []
        cutoff = datetime.now().astimezone() - timedelta(days=self.settings.retention_days)
        removed: list[str] = []
        for item in self.list():
            try:
                cleaned = datetime.fromisoformat(str(item.get("cleaned_at") or ""))
            except ValueError:
                continue
            if cleaned <= cutoff:
                self.delete(str(item["name"]))
                removed.append(str(item["name"]))
        return removed

    def _entry(self, name: str) -> tuple[Path, dict[str, Any]]:
        directory = (self.paths.trash / name).resolve()
        if not directory.is_relative_to(self.paths.trash.resolve()) or directory == self.paths.trash.resolve():
            raise ApiError(400, "TRASH_NAME_INVALID", "非法回收站条目")
        if not directory.is_dir():
            raise ApiError(404, "TRASH_NOT_FOUND", f"回收站条目不存在：{name}")
        meta = _read_json(directory / "meta.json")
        if not meta:
            meta = {"name": name, "legacy": True}
        return directory, meta

    @staticmethod
    def _write_meta(directory: Path, data: dict[str, Any]) -> None:
        temporary = directory / "meta.json.tmp"
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(directory / "meta.json")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _size(path: Path) -> int:
    if not path.is_dir():
        return path.lstat().st_size if path.exists() else 0
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file() and not item.is_symlink():
                total += item.lstat().st_size
        except OSError:
            continue
    return total


def _now() -> str:
    return datetime.now().astimezone().replace(microsecond=0).isoformat()


def _expires(days: int) -> str:
    if days <= 0:
        return ""
    return (datetime.now().astimezone() + timedelta(days=days)).replace(microsecond=0).isoformat()


def _remaining_days(value: str) -> int | None:
    if not value:
        return None
    try:
        delta = datetime.fromisoformat(value) - datetime.now().astimezone()
    except ValueError:
        return None
    return max(0, int(delta.total_seconds() // 86400))
