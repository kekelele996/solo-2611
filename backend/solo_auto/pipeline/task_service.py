"""Task creation and editable console-side drafts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from solo_auto.errors import ApiError
from solo_auto.events.bus import EventBus
from solo_auto.gateway.skill import SkillGateway
from solo_auto.pipeline.reconcile import Reconciler
from solo_auto.settings import Paths, Settings
from solo_auto.sources.staging import StagingManager
from solo_auto.store.repo import Repository
from solo_auto.store.state import load_state

if TYPE_CHECKING:
    from solo_auto.pipeline.account_dispatch import AccountDispatch


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
        self.routing: AccountDispatch | None = None

    def attach_ledger(self, ledger: Any) -> None:
        self.ledger = ledger

    async def create(
        self,
        *,
        staging_id: str,
        task_name: str | None = None,
        domain: str | None = None,
        sub_direction: str | None = None,
        staging: StagingManager | None = None,
        ledger: Any = None,
    ) -> dict[str, Any]:
        staging = staging or self.staging
        ledger = ledger or self.ledger
        meta = staging.get(staging_id)
        route = meta.get("accountRoute")
        routing = self.routing
        if routing and routing.enabled and not route:
            raise ApiError(409, "TASK_SOURCE_REVIEW", "请从主号工程池重新领取工程，旧的暂存记录缺少账号凭证")
        if route:
            assert routing is not None
            claim = routing.row(claim_key=meta["claimKey"])
            if (not claim or claim["claim_state"] != "held" or claim["task_id"]
                    or claim["meta"] != meta):
                raise ApiError(409, "TASK_SOURCE_REVIEW", "暂存工程与领取记录不一致或已经建题")
            if domain and domain != route["domain"]:
                raise ApiError(422, "TASK_DOMAIN_MISMATCH", "所选领域与任务账号的绑定领域不一致")
        name = (task_name or str(meta.get("name") or staging_id)).strip()
        if not name:
            raise ApiError(422, "TASK_NAME_REQUIRED", "任务名不能为空")
        source_domain, source_sub_direction = _classification(meta)
        domain = route["domain"] if route else (domain or "").strip() or source_domain
        sub_direction = (sub_direction or "").strip() or (source_sub_direction if source_domain == domain else "")
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
        if domain:
            argv.extend(["--domain", domain])
        if sub_direction:
            argv.extend(["--sub-direction", sub_direction])
        if route:
            assert routing is not None
            with staging.verified_source(staging_id, meta["zipSha256"]) as fresh_source:
                argv[argv.index("--source") + 1] = str(fresh_source)
                argv.extend(["--account-claim", meta["claimKey"]])
                routing.save_claim({**meta, "initSourcePath": str(fresh_source)})
                try:
                    result = await self.gateway.call(argv, timeout=1800)
                finally:
                    routing.save_claim(meta)
        else:
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
        if route:
            assert routing is not None
            meta["initialSha"] = str(result.data.get("initialSha") or "")
            envelope = load_state(task_root)
            state = envelope.raw
            if (task_root.parent != self.settings.tasks_dir.resolve() or not envelope.compatible or not meta["initialSha"]
                    or state.get("initialSha") != meta["initialSha"]
                    or state.get("accountRoute") != route or state.get("accountClaim") != meta["claimKey"]):
                raise ApiError(409, "TASK_SOURCE_REVIEW", "初始化返回的工作目录、快照或账号凭证不一致")
            meta["taskRoot"] = str(task_root)
            routing.save_claim(meta, task_id)
        _write_workbench_marker(
            task_root,
            task_id=task_id,
            meta=meta,
            host=self.settings.host,
            port=self.settings.port,
        )
        staging.attach_task(staging_id, task_root, extra=meta if route else None)
        if route:
            await self.reconciler.run()
            self.repo.execute("UPDATE claims SET task_id=? WHERE id=?", (task_id, meta["claimKey"]))
        drafts = self.paths.drafts / task_id
        drafts.mkdir(parents=True, exist_ok=True)
        template_result = await self.gateway.call(
            ["prompt", "--task-root", str(task_root), "--init"],
            timeout=120,
            task_id=task_id,
        )
        if template_result.code != 0 and not route:
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
        if template_result.code != 0:
            self.repo.add_event("task.prompt_templates_deferred", "任务已建，题面模板将在出题阶段重新核对",
                                level="warn", task_id=task_id)
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
        if ledger:
            ledger.attach_task(meta, task_id, name)
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
        if not route:
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


def _write_workbench_marker(
    task_root: Path,
    *,
    task_id: str,
    meta: dict[str, Any],
    host: str,
    port: int,
) -> None:
    api_host = host if host not in {"0.0.0.0", "::", ""} else "127.0.0.1"
    path = task_root / "monitor" / "workbench.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema": "solo-auto/workbench-task/1",
                "taskId": task_id,
                "taskRoot": str(task_root),
                "projectId": str(meta.get("projectId") or ""),
                "projectName": str(meta.get("projectName") or ""),
                "engineeringId": str(meta.get("engineeringId") or ""),
                "engineeringName": str(meta.get("name") or ""),
                **({key: meta[key] for key in ("accountRoute", "claimKey", "zipSha256", "initialSha")} if meta.get("accountRoute") else {}),
                "apiBase": f"http://{api_host}:{port}",
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _classification(meta: dict[str, Any]) -> tuple[str, str]:
    tags = meta.get("tags")
    if not isinstance(tags, list):
        return "", ""
    for tag in tags:
        if not isinstance(tag, dict):
            continue
        domain = str(tag.get("parent_name") or "").strip()
        sub_direction = str(tag.get("name") or "").strip()
        if domain and sub_direction:
            return domain, sub_direction
    return "", ""


def _now() -> str:
    from datetime import datetime

    return datetime.now().astimezone().replace(microsecond=0).isoformat()
