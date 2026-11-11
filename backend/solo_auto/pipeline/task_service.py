"""Task creation and editable console-side drafts."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from solo_auto.errors import ApiError
from solo_auto.events.bus import EventBus
from solo_auto.gateway.skill import SkillGateway
from solo_auto.pipeline.reconcile import Reconciler
from solo_auto.settings import Paths, Settings
from solo_auto.sources.staging import StagingManager
from solo_auto.store.repo import Repository


class TaskService:
    def __init__(
        self,
        settings: Settings,
        paths: Paths,
        gateway: SkillGateway,
        repo: Repository,
        staging: StagingManager,
        reconciler: Reconciler,
        bus: EventBus,
    ) -> None:
        self.settings = settings
        self.paths = paths
        self.gateway = gateway
        self.repo = repo
        self.staging = staging
        self.reconciler = reconciler
        self.bus = bus
        self.ledger: Any = None

    def attach_ledger(self, ledger: Any) -> None:
        self.ledger = ledger

    async def create(
        self,
        *,
        staging_id: str,
        task_name: str | None = None,
        domain: str | None = None,
        sub_direction: str | None = None,
    ) -> dict[str, Any]:
        meta = self.staging.get(staging_id)
        name = (task_name or str(meta.get("name") or staging_id)).strip()
        if not name:
            raise ApiError(422, "TASK_NAME_REQUIRED", "任务名不能为空")
        source_path = Path(str(meta["sourcePath"])).resolve()
        argv = [
            "init",
            "--task-name",
            name,
            "--source",
            str(source_path),
            "--tasks-dir",
            str(self.settings.tasks_dir),
        ]
        result = await self.gateway.call(argv, timeout=1800)
        if result.code != 0 or not result.data or not result.data.get("taskRoot"):
            raise ApiError(
                422 if result.code == 2 else 500,
                "TASK_INIT_FAILED",
                "任务初始化失败",
                detail={"stdout": result.out, "stderr": result.err, "exit_code": result.code},
            )
        task_root = Path(str(result.data["taskRoot"])).resolve()
        task_id = self.repo.task_id_for_root(task_root)
        drafts = self.paths.drafts / task_id
        drafts.mkdir(parents=True, exist_ok=True)
        draft_path = drafts / "题面草稿.md"
        if not draft_path.exists():
            draft_path.write_text(
                "把真实项目里的业务场景、现有流程、两条互相牵制的约束，以及实现错误时出现的可见结果，"
                "写成一段自然的需求说明。不要写文件路径、端口、技术栈或逐步修复指令。\n",
                encoding="utf-8",
            )
        template_result = await self.gateway.call(
            ["prompt", "--task-root", str(task_root), "--init"],
            timeout=120,
            task_id=task_id,
        )
        if template_result.code != 0:
            raise ApiError(
                500,
                "PROMPT_TEMPLATES_FAILED",
                template_result.err.strip() or "题面证据模板初始化失败",
                detail={
                    "task_root": str(task_root),
                    "stdout": template_result.out,
                    "stderr": template_result.err,
                },
            )
        template_data = template_result.data or {}
        templates = template_data.get("templates")
        if not isinstance(templates, dict):
            templates = {
                "difficultyReview": str(task_root / "monitor" / "prompt" / "difficulty-review.json"),
                "raRenhuaReview": str(task_root / "monitor" / "prompt" / "ra-renhua-review.json"),
            }
        for source_key, target_name in (
            ("difficultyReview", "difficulty-review.json"),
            ("raRenhuaReview", "ra-renhua-review.json"),
        ):
            source = Path(str(templates.get(source_key) or ""))
            target = drafts / target_name
            if source.is_file() and not target.exists():
                target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        self.staging.attach_task(staging_id, task_root)
        if self.ledger:
            self.ledger.attach_task(meta, task_id, name)
        self.repo.add_event(
            "task.created",
            f"{name} 已创建",
            actor="user",
            task_id=task_id,
            details={
                "task_root": str(task_root),
                "staging_id": staging_id,
                "domain": domain,
                "sub_direction": sub_direction,
            },
        )
        await self.reconciler.run()
        self.bus.publish(
            "task.updated",
            {"task_id": task_id, "job_status": "authored", "step": 1},
        )
        return {
            "task_id": task_id,
            "task_root": str(task_root),
            "initial_sha": result.data.get("initialSha", ""),
        }


def task_id_for_root(task_root: Path) -> str:
    return str(__import__("uuid").uuid5(__import__("uuid").NAMESPACE_URL, str(task_root.resolve())))


def _now() -> str:
    from datetime import datetime

    return datetime.now().astimezone().replace(microsecond=0).isoformat()
