"""Export orchestration with unavoidable skill-owned workbook generation."""

from __future__ import annotations

import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from solo_auto.errors import ApiError
from solo_auto.events.bus import EventBus
from solo_auto.gateway.skill import SkillGateway
from solo_auto.pipeline.reconcile import Reconciler
from solo_auto.pipeline.steps import evaluate_steps
from solo_auto.settings import Paths
from solo_auto.store.repo import Repository


class ExportService:
    def __init__(
        self,
        paths: Paths,
        gateway: SkillGateway,
        repo: Repository,
        reconciler: Reconciler,
        bus: EventBus,
    ) -> None:
        self.paths = paths
        self.gateway = gateway
        self.repo = repo
        self.reconciler = reconciler
        self.bus = bus

    async def export(
        self,
        *,
        task_ids: list[str] | None = None,
        date: str | None = None,
    ) -> dict[str, Any]:
        if not task_ids and not date:
            date = datetime.now().strftime("%Y-%m-%d")
        tasks: list[dict[str, Any]] = []
        if task_ids:
            for task_id in task_ids:
                task = self.repo.get_task(task_id)
                if not task:
                    raise ApiError(404, "TASK_NOT_FOUND", f"任务不存在：{task_id}")
                tasks.append(task)
        else:
            tasks, _ = self.repo.list_tasks(date=date, page=1, page_size=1000)
        selected: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for task in tasks:
            root = Path(task["task_root"])
            evaluation = evaluate_steps(root, duplicate_sessions=self.repo.session_counts())
            # 第 11 项就是本次要生成的 Excel，导出前只要求前十项通过
            blockers = [step for step in evaluation.steps[:10] if not step.ok]
            if blockers:
                reasons = [item for item in evaluation.missing if not item.startswith("11.")]
                skipped.append({"task_id": task["id"], "task": task["task_name"], "reason": reasons})
                continue
            export_result = await self.gateway.call(
                ["export", "--task-root", str(root)],
                timeout=300,
                task_id=task["id"],
            )
            if export_result.code == 2:
                data = export_result.data or {}
                skipped.append(
                    {
                        "task_id": task["id"],
                        "task": task["task_name"],
                        "reason": data.get("missing") or [export_result.err or "导出缺项"],
                    }
                )
                continue
            if export_result.code != 0:
                skipped.append(
                    {
                        "task_id": task["id"],
                        "task": task["task_name"],
                        "reason": [export_result.err.strip() or "技能导出失败"],
                    }
                )
                continue
            selected.append(task)
        if not selected:
            return {"file": "", "rows": 0, "skipped": skipped}
        self.paths.exports.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = self.paths.exports / f"交付表-{date or stamp}-{stamp}.xlsx"
        with tempfile.TemporaryDirectory(prefix="solo-auto-export-") as temp:
            temp_root = Path(temp)
            for task in selected:
                (temp_root / task["id"]).symlink_to(Path(task["task_root"]), target_is_directory=True)
            result = await self.gateway.call(
                ["export-batch", "--tasks-dir", str(temp_root), "--out", str(target)],
                timeout=600,
            )
        data = result.data or {}
        if result.code != 0 or not data.get("ok"):
            raise ApiError(
                500,
                "EXPORT_FAILED",
                result.err.strip() or "批量导出失败",
                detail={"stdout": result.out, "stderr": result.err, "data": data},
            )
        skipped.extend(_normalize_skips(data.get("skipped") or [], selected))
        await self.reconciler.run()
        for task in selected:
            self.repo.add_event(
                "task.exported",
                f"{task['task_name']} 已导出",
                actor="user",
                task_id=task["id"],
                details={"file": str(target)},
            )
        self.bus.publish("export.updated", {"file": str(target), "rows": int(data.get("rows") or 0)})
        return {"file": str(target), "rows": int(data.get("rows") or 0), "skipped": skipped}

    def history(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for path in sorted(self.paths.exports.glob("*.xlsx"), key=lambda item: item.stat().st_mtime, reverse=True):
            items.append(
                {
                    "name": path.name,
                    "path": str(path),
                    "size": path.stat().st_size,
                    "created_at": datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(),
                }
            )
        return items

    def preview(self, name: str) -> dict[str, Any]:
        path = self._safe_export_path(name)
        from openpyxl import load_workbook  # type: ignore[import-untyped]

        workbook = load_workbook(path, read_only=True, data_only=True)
        sheet = workbook["提交表"]
        rows = list(sheet.iter_rows(values_only=True))
        headers = list(rows[1]) if len(rows) > 1 else []
        body = [list(row) for row in rows[2:]]
        return {"headers": headers, "rows": body}

    def path(self, name: str) -> Path:
        return self._safe_export_path(name)

    def _safe_export_path(self, name: str) -> Path:
        path = (self.paths.exports / name).resolve()
        if not path.is_relative_to(self.paths.exports.resolve()) or path.suffix != ".xlsx":
            raise ApiError(400, "EXPORT_PATH_INVALID", "非法导出文件名")
        if not path.is_file():
            raise ApiError(404, "EXPORT_NOT_FOUND", f"导出文件不存在：{name}")
        return path


def _normalize_skips(items: list[Any], selected: list[dict[str, Any]]) -> list[dict[str, Any]]:
    roots = {str(task["task_root"]): task for task in selected}
    output: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        root = str(item.get("task") or "")
        task = roots.get(root)
        output.append(
            {
                "task_id": task["id"] if task else "",
                "task": task["task_name"] if task else root,
                "reason": item.get("missing") or [item.get("reason") or "未知原因"],
            }
        )
    return output
