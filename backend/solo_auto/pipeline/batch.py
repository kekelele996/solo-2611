"""Submission batch checks, generation and portable bundles."""

from __future__ import annotations

import difflib
import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from solo_auto.errors import ApiError
from solo_auto.events.bus import EventBus
from solo_auto.pipeline.export_service import ExportService
from solo_auto.pipeline.steps import evaluate_steps
from solo_auto.pipeline.usage import UsageLedger
from solo_auto.settings import Paths
from solo_auto.store.repo import Repository
from solo_auto.store.state import load_state


class BatchService:
    def __init__(
        self,
        *,
        paths: Paths,
        repo: Repository,
        exports: ExportService,
        ledger: UsageLedger,
        bus: EventBus,
    ) -> None:
        self.paths = paths
        self.repo = repo
        self.exports = exports
        self.ledger = ledger
        self.bus = bus

    def check(self, task_ids: list[str], *, include_exported: bool = False) -> dict[str, Any]:
        selected = self._tasks(task_ids)
        blockers: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []
        session_map: dict[str, list[str]] = {}
        prompt_map: dict[str, list[str]] = {}
        prompts: list[tuple[str, str]] = []
        unaudited = 0
        for task in selected:
            task_id = str(task["id"])
            root = Path(str(task["task_root"]))
            evaluation = evaluate_steps(root, duplicate_sessions=self.repo.session_counts())
            missing = [item for item in evaluation.missing if not item.startswith("10.")]
            if missing:
                blockers.append(
                    {"code": "DELIVERY_INCOMPLETE", "task_id": task_id, "task": task["task_name"], "message": "；".join(missing)}
                )
            envelope = load_state(root)
            state = envelope.state
            if state is None:
                blockers.append(
                    {
                        "code": "STATE_INCOMPATIBLE",
                        "task_id": task_id,
                        "task": task["task_name"],
                        "message": envelope.error or "state.json 不可读",
                    }
                )
                continue
            session_id = state.session.sessionId if state.session else ""
            prompt_id = state.session.promptId if state.session else ""
            if session_id:
                session_map.setdefault(session_id, []).append(task_id)
            if prompt_id:
                prompt_map.setdefault(prompt_id, []).append(task_id)
            prompt = _read_text(Path(state.promptPath)) if state.promptPath else ""
            if prompt:
                prompts.append((task_id, prompt))
            rec = state.recording
            video = Path(rec.video) if rec and rec.video else None
            if video and not video.is_file():
                blockers.append(
                    {
                        "code": "FILE_MISSING",
                        "task_id": task_id,
                        "task": task["task_name"],
                        "message": f"录屏文件不存在：{video}",
                    }
                )
            for path in _referenced_files(envelope.raw):
                if not path.is_file():
                    blockers.append(
                        {
                            "code": "FILE_MISSING",
                            "task_id": task_id,
                            "task": task["task_name"],
                            "message": f"表内引用文件不存在：{path}",
                        }
                    )
            audit = self.repo.audit(task_id)
            ack_by = str(task.get("ack_by") or "")
            if ack_by != "user":
                unaudited += 1
            if audit and audit.get("sampled") and audit.get("verdict") != "pass":
                blockers.append(
                    {
                        "code": "AUDIT_NOT_PASSED",
                        "task_id": task_id,
                        "task": task["task_name"],
                        "message": "被抽中但尚未通过人工抽检",
                    }
                )
            if not include_exported and self.repo.task_in_any_batch(task_id):
                blockers.append(
                    {
                        "code": "ALREADY_IN_BATCH",
                        "task_id": task_id,
                        "task": task["task_name"],
                        "message": "该任务已进入历史批次；如需重复导出，请显式勾选「包含已导出」",
                    }
                )
            if not self.repo.task_in_any_batch(task_id):
                warnings.append(
                    {
                        "code": "FIRST_SUBMISSION",
                        "task_id": task_id,
                        "task": task["task_name"],
                        "message": "将首次计入提交次数",
                    }
                )
        for session_id, ids in session_map.items():
            if len(ids) > 1:
                blockers.append(
                    {
                        "code": "DUPLICATE_SESSION",
                        "task_id": ids[0],
                        "task": ", ".join(ids),
                        "message": f"SessionID 重复：{session_id}",
                    }
                )
        for prompt_id, ids in prompt_map.items():
            if len(ids) > 1:
                blockers.append(
                    {
                        "code": "DUPLICATE_PROMPT",
                        "task_id": ids[0],
                        "task": ", ".join(ids),
                        "message": f"PromptID 重复：{prompt_id}",
                    }
                )
        for index, (left_id, left) in enumerate(prompts):
            for right_id, right in prompts[index + 1 :]:
                ratio = difflib.SequenceMatcher(None, left, right).ratio()
                if ratio >= 0.92:
                    warnings.append(
                        {
                            "code": "SIMILAR_PROMPT",
                            "task_id": left_id,
                            "task": f"{left_id}, {right_id}",
                            "message": f"题面相似度 {ratio:.0%}，请人工确认是否同质化",
                        }
                    )
        if unaudited:
            warnings.append(
                {
                    "code": "UNAUDITED",
                    "task_id": "",
                    "task": "",
                    "message": f"本批次含 {unaudited} 条未经人工抽检通过的数据",
                }
            )
        return {
            "ok": not blockers,
            "task_ids": [str(task["id"]) for task in selected],
            "blockers": blockers,
            "warnings": warnings,
            "unaudited_count": unaudited,
            "checked_at": _now(),
        }

    async def create(
        self,
        task_ids: list[str],
        *,
        include_exported: bool = False,
        acknowledge_warnings: bool = False,
    ) -> dict[str, Any]:
        report = self.check(task_ids, include_exported=include_exported)
        if not report["ok"]:
            raise ApiError(
                409,
                "BATCH_CHECK_FAILED",
                "批次体检未通过",
                detail=report,
            )
        if report["warnings"] and not acknowledge_warnings:
            raise ApiError(
                400,
                "BATCH_WARNINGS_UNACKNOWLEDGED",
                "存在提示项，请确认后再生成批次",
                detail=report,
            )
        result = await self.exports.export(task_ids=task_ids)
        if not result.get("file"):
            raise ApiError(409, "BATCH_EXPORT_SKIPPED", "没有生成提交表", detail=result)
        batch_id = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        directory = self.paths.batches / batch_id
        directory.mkdir(parents=True, exist_ok=False)
        xlsx = directory / Path(str(result["file"])).name
        shutil.copy2(str(result["file"]), xlsx)
        record = {
            "id": batch_id,
            "created_at": _now(),
            "xlsx_path": str(xlsx),
            "rows": int(result.get("rows") or 0),
            "task_ids": report["task_ids"],
            "check": report,
            "skipped": result.get("skipped") or [],
        }
        self._write_manifest(directory, record)
        self.repo.insert_batch(
            batch_id=batch_id,
            created_at=record["created_at"],
            xlsx_path=str(xlsx),
            check=report,
            task_ids=report["task_ids"],
            unaudited_count=int(report["unaudited_count"]),
        )
        for task_id in report["task_ids"]:
            self.ledger.mark_submitted(task_id)
            self.repo.add_event(
                "batch.submitted",
                "任务已进入提交批次",
                actor="user",
                task_id=task_id,
                details={"batch_id": batch_id},
            )
        self.bus.publish("batch.updated", {"batch_id": batch_id, "rows": record["rows"]})
        saved = self.repo.batch(batch_id)
        if saved is None:
            raise ApiError(500, "BATCH_SAVE_FAILED", "批次已生成但索引写入失败")
        return {**record, **saved}

    def bundle(self, batch_id: str) -> dict[str, Any]:
        batch = self.repo.batch(batch_id)
        if not batch:
            raise ApiError(404, "BATCH_NOT_FOUND", f"批次不存在：{batch_id}")
        directory = self.paths.batches / batch_id
        files_dir = directory / "files"
        files_dir.mkdir(parents=True, exist_ok=True)
        copied: list[str] = []
        for task_id in batch["task_ids"]:
            task = self.repo.get_task(task_id)
            if not task:
                continue
            envelope = load_state(Path(str(task["task_root"])))
            rec = envelope.state.recording if envelope.state else None
            candidates = [Path(rec.video)] if rec and rec.video else []
            candidates.extend(_referenced_files(envelope.raw))
            for source in candidates:
                if not source.is_file():
                    continue
                destination = files_dir / task_id / source.name
                destination.parent.mkdir(parents=True, exist_ok=True)
                if not destination.exists():
                    shutil.copy2(source, destination)
                    copied.append(str(destination))
        xlsx = Path(str(batch["xlsx_path"]))
        if xlsx.is_file():
            copied.append(str(xlsx))
        target = directory / "提交包.zip"
        target.unlink(missing_ok=True)
        archive = shutil.make_archive(str(directory / "提交包"), "zip", root_dir=directory, base_dir=None)
        return {
            "batch_id": batch_id,
            "bundle": archive,
            "files": copied,
            "note": "技能未支持把表内绝对路径改写为相对路径；本次只收拢文件，未改动 xlsx。",
        }

    def _tasks(self, task_ids: list[str]) -> list[dict[str, Any]]:
        unique = list(dict.fromkeys(task_ids))
        if not unique:
            raise ApiError(422, "TASK_IDS_REQUIRED", "请至少选择一个任务")
        tasks: list[dict[str, Any]] = []
        for task_id in unique:
            task = self.repo.get_task(task_id)
            if not task:
                raise ApiError(404, "TASK_NOT_FOUND", f"任务不存在：{task_id}")
            tasks.append(task)
        return tasks

    def _write_manifest(self, directory: Path, data: dict[str, Any]) -> None:
        (directory / "manifest.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (directory / "check.json").write_text(
            json.dumps(data["check"], ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


def _referenced_files(raw: dict[str, Any]) -> list[Path]:
    found: set[Path] = set()

    def visit(value: Any) -> None:
        if isinstance(value, str) and value.startswith("/"):
            path = Path(value)
            if path.suffix and "://" not in value:
                found.add(path)
        elif isinstance(value, dict):
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(raw)
    return sorted(found)


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _now() -> str:
    return datetime.now().astimezone().replace(microsecond=0).isoformat()
