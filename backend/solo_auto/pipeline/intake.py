"""Production plans that refill one exclusive claim at a time."""

from __future__ import annotations

import asyncio
import json
import shutil
import statistics
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from solo_auto.errors import ApiError, SourceAuthError
from solo_auto.events.bus import EventBus
from solo_auto.pipeline.autopilot import Autopilot
from solo_auto.pipeline.notify import Notifier
from solo_auto.pipeline.task_service import TaskService
from solo_auto.pipeline.usage import UsageLedger
from solo_auto.settings import IntakeSettings, Paths, Settings
from solo_auto.sources.project_hub import ProjectHubClient
from solo_auto.sources.staging import StagingManager
from solo_auto.store.repo import Repository


class IntakeService:
    def __init__(
        self,
        *,
        paths: Paths,
        settings: Settings,
        repo: Repository,
        source: ProjectHubClient,
        staging: StagingManager,
        tasks: TaskService,
        autopilot: Autopilot,
        ledger: UsageLedger,
        bus: EventBus,
        notifier: Notifier,
    ) -> None:
        self.paths = paths
        self.settings = settings
        self.intake_settings: IntakeSettings = settings.intake
        self.repo = repo
        self.source = source
        self.staging = staging
        self.tasks = tasks
        self.autopilot = autopilot
        self.ledger = ledger
        self.bus = bus
        self.notifier = notifier
        self.state_file = paths.home / "intake.json"
        self.plan: dict[str, Any] | None = None
        self._loop_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._load()

    async def start(self) -> None:
        if self._loop_task is None:
            self._loop_task = asyncio.create_task(self._loop())

    async def shutdown(self) -> None:
        if self._loop_task is not None:
            self._loop_task.cancel()
            await asyncio.gather(self._loop_task, return_exceptions=True)
            self._loop_task = None

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception as exc:
                self.repo.add_event(
                    "intake.failed",
                    f"{type(exc).__name__}: {exc}",
                    level="error",
                    actor="intake",
                )
            await asyncio.sleep(3)

    def snapshot(self) -> dict[str, Any]:
        if not self.plan:
            return {
                "plan": None,
                "counts": {
                    "target": 0,
                    "completed": 0,
                    "active": 0,
                    "failed": 0,
                    "recoverable": 0,
                    "orphaned": 0,
                    "claimed": 0,
                    "staged": 0,
                    "in_hand": 0,
                },
                "max_in_hand": self.max_in_hand,
                "eta": "",
            }
        counts = self._counts(self.plan)
        target = int(self.plan.get("target") or 0)
        durations = [
            float(job.get("elapsed_seconds") or 0)
            for task_id in self.plan.get("task_ids") or []
            if (job := self.autopilot.job(str(task_id)))
            and job.get("stage") in {"done", "awaiting_audit", "failed"}
        ]
        eta = ""
        if len(durations) >= 3 and counts["completed"] < target:
            remaining = target - counts["completed"]
            median = statistics.median(durations[-10:])
            concurrency = max(1, min(self.max_in_hand, self.settings.execution.max_containers))
            seconds = median * remaining / concurrency
            eta = (datetime.now().astimezone() + timedelta(seconds=seconds)).replace(microsecond=0).isoformat()
        return {
            **self.plan,
            "plan": self.plan,
            "counts": counts,
            "max_in_hand": self.max_in_hand,
            "eta": eta,
            "attempts_left": max(0, int(self.plan.get("claim_limit") or 0) - len(self.plan.get("task_ids") or [])
                                  - len(self.plan.get("failed_claims") or [])),
        }

    async def create(
        self,
        *,
        project_id: str,
        target: int,
        filters: dict[str, Any] | None = None,
        exclude_delivered: bool = True,
        max_uses: int = 0,
    ) -> dict[str, Any]:
        if not project_id.strip():
            raise ApiError(400, "PROJECT_REQUIRED", "创建生产计划前必须选择项目")
        if self.plan and self.plan.get("status") in {"running", "paused"}:
            raise ApiError(409, "INTAKE_ACTIVE", "已有进行中的生产计划")
        if target < 1 or target > 15:
            raise ApiError(400, "INTAKE_TARGET_INVALID", "单个项目的目标题数必须在 1–15 之间")
        projects = await self.source.list_projects()
        project = next((item for item in projects if str(item.get("id") or "") == project_id), None)
        if project is None:
            raise ApiError(404, "PROJECT_NOT_FOUND", f"项目不存在或无权访问：{project_id}")
        self.plan = {
            "id": datetime.now().strftime("%Y%m%d-%H%M%S"),
            "project_id": project_id,
            "project_name": str(project.get("name") or project_id),
            "filters": filters or {},
            "target": target,
            "status": "running",
            "created_at": _now(),
            "updated_at": _now(),
            "task_ids": [],
            "claimed_engineering_ids": [],
            "attempted_engineering_ids": [],
            "failed_claims": [],
            "exclude_delivered": exclude_delivered,
            "max_uses": max_uses,
            "claim_limit": max(target * 2, target + 3),
        }
        self._save()
        self.repo.add_event(
            "intake.started",
            f"生产计划启动：目标 {target} 题",
            actor="user",
            details={"project_id": project_id, "filters": filters or {}},
        )
        await self.tick()
        return self.snapshot()

    async def pause(self, reason: str = "已手动暂停") -> dict[str, Any]:
        plan = self._require_plan()
        plan["status"] = "paused"
        plan["pause_reason"] = reason
        plan["updated_at"] = _now()
        self._save()
        self.bus.publish("intake.updated", {"status": "paused", "reason": reason})
        return self.snapshot()

    def block(self, reason: str) -> None:
        if not self.plan or self.plan.get("status") not in {"running", "paused"}:
            return
        self.plan["status"] = "paused"
        self.plan["pause_reason"] = reason
        self._save()
        self.bus.publish("intake.updated", {"status": "paused", "reason": reason})

    async def resume(self) -> dict[str, Any]:
        plan = self._require_plan()
        if plan.get("status") in {"stopped", "completed"}:
            raise ApiError(409, "INTAKE_FINISHED", "已结束的计划不能恢复，请新建计划")
        source_auth = self.source.test()
        result = await source_auth
        if not result.get("ok"):
            raise ApiError(409, "PREFLIGHT_FAILED", str(result.get("message") or "题源登录态不可用"))
        recovery = await self.autopilot.start_tasks(
            [str(task_id) for task_id in plan.get("task_ids") or []]
        )
        plan["status"] = "running"
        plan.pop("pause_reason", None)
        plan["updated_at"] = _now()
        self._save()
        self.repo.add_event(
            "intake.resumed",
            f"生产计划继续；重新拉起 {len(recovery.get('recovered') or [])} 题，"
            f"补入队列 {len(recovery.get('added') or [])} 题",
            actor="user",
            details={
                "recovered": recovery.get("recovered") or [],
                "added": recovery.get("added") or [],
                "skipped": recovery.get("skipped") or [],
            },
        )
        self.bus.publish("intake.updated", {"status": "running", "counts": self._counts(plan)})
        return {**self.snapshot(), "recovery": recovery}

    async def stop(self, *, release_unstarted: bool = False) -> dict[str, Any]:
        plan = self._require_plan()
        plan["status"] = "stopped"
        plan["stopped_at"] = _now()
        plan["updated_at"] = _now()
        released: list[str] = []
        if release_unstarted:
            for item in self.staging.list():
                if str(item.get("projectId") or "") != str(plan["project_id"]) or item.get("taskRoot"):
                    continue
                try:
                    await self.source.release(
                        str(item["projectId"]),
                        str(item["engineeringId"]),
                        str(item["claimId"]),
                    )
                    self.ledger.mark_released(str(item["projectId"]), str(item["engineeringId"]))
                    self.staging.remove(str(item["engineeringId"]))
                    released.append(str(item["engineeringId"]))
                except Exception as exc:
                    self.repo.add_event(
                        "intake.release_failed",
                        f"{item.get('name') or item.get('engineeringId')} 归还失败：{exc}",
                        level="warn",
                        actor="user",
                    )
        plan["released"] = released
        self._save()
        self.repo.add_event(
            "intake.stopped",
            f"生产计划已终止；归还 {len(released)} 个未开工工程",
            actor="user",
        )
        return self.snapshot()

    async def tick(self) -> None:
        async with self._lock:
            plan = self.plan
            if not plan or plan.get("status") != "running":
                return
            if not self.settings.autopilot.enabled:
                plan["status"] = "paused"
                plan["pause_reason"] = "自动交付总开关已关闭"
                plan["updated_at"] = _now()
                self._save()
                return
            if self.autopilot.breaker.tripped:
                plan["status"] = "paused"
                plan["pause_reason"] = f"自动交付已熔断：{self.autopilot.breaker.kind or '连续失败'}"
                plan["updated_at"] = _now()
                self._save()
                return
            if self.autopilot.paused:
                return
            dispatcher = getattr(self.autopilot, "dispatcher", None)
            if dispatcher is not None and dispatcher.v3_enabled:
                if dispatcher.desired_state != "running" and not dispatcher.config.refill.refill_while_paused:
                    return
                backpressure = self.repo.query_one(
                    """
                    SELECT
                      (SELECT COUNT(*) FROM tasks WHERE job_status='blocked')
                      + (SELECT COUNT(*) FROM dispatch_items WHERE status='quarantined')
                      + (SELECT COUNT(*) FROM audits WHERE sampled=1 AND verdict IS NULL) AS n
                    """
                ) or {"n": 0}
                backlog = int(backpressure.get("n") or 0)
                threshold = dispatcher.config.refill.backpressure_threshold
                if backlog <= threshold * 0.7 and "backpressure" in dispatcher.holds.holds:
                    dispatcher.release_hold("backpressure", actor="backpressure")
                hold = dispatcher.holds.blocking(["intake"])
                if hold is not None:
                    return
                if backlog > threshold:
                    dispatcher.register_hold(
                        source="backpressure",
                        scope=["intake"],
                        reason_code="backpressure",
                        reason=f"待处理积压 {backlog}，超过阈值 {threshold}",
                        auto=True,
                    )
                    return
            await self._repair_missing_jobs(plan)
            await self._release_finished_claims(plan)
            counts = self._counts(plan)
            target = int(plan.get("target") or 0)
            if counts["completed"] >= target:
                plan["status"] = "completed"
                plan["updated_at"] = _now()
                self._save()
                self.notifier.notify(title="solo-auto 生产计划", status=f"完成 {counts['completed']} 题")
                return
            if len(plan.get("task_ids") or []) + len(plan.get("failed_claims") or []) >= int(
                plan.get("claim_limit") or 0
            ):
                plan["status"] = "paused"
                plan["pause_reason"] = "达到领取上限，已熔断"
                plan["updated_at"] = _now()
                self._save()
                self.repo.add_event(
                    "intake.breaker",
                    "生产计划达到领取上限，已暂停",
                    level="error",
                    actor="intake",
                )
                self.notifier.notify(title="solo-auto 计划暂停", status="达到领取上限")
                return
            staged = self._pending_staged(plan)
            remaining_capacity = max(
                0,
                min(self.max_in_hand, target - counts["completed"]) - counts["active"],
            )
            if staged:
                if remaining_capacity > 0:
                    await self._adopt_staged(plan, staged[0])
                else:
                    await self._release_staged_claim(staged[-1], "超过在手上限，已归还，等待后续补位")
                return
            remaining_target = max(0, target - counts["completed"])
            if self._in_hand(plan) >= min(self.max_in_hand, remaining_target):
                return
            free_gb = shutil.disk_usage(self.paths.home).free / 1024**3
            if free_gb < self.settings.storage.disk_floor_gb:
                if dispatcher is not None and dispatcher.v3_enabled:
                    dispatcher.register_hold(
                        source="disk_guard",
                        scope=["containers", "intake"],
                        reason_code="disk.low",
                        reason=f"磁盘剩余 {free_gb:.1f}GB，低于安全阈值",
                        auto=True,
                    )
                    return
                plan["status"] = "paused"
                plan["pause_reason"] = f"磁盘剩余 {free_gb:.1f}GB，低于安全阈值"
                plan["updated_at"] = _now()
                self._save()
                self.notifier.notify(title="solo-auto 计划暂停", status="磁盘空间不足")
                return
            try:
                candidate = await self._next_candidate(plan)
            except SourceAuthError as exc:
                if dispatcher is not None and dispatcher.v3_enabled:
                    dispatcher.register_hold(
                        source="source_guard",
                        scope=["intake"],
                        reason_code="source.auth",
                        reason=f"题源会话过期：{exc}",
                        auto=True,
                    )
                    self.repo.add_event(
                        "source.auth_expired",
                        str(exc),
                        level="error",
                        actor="intake",
                    )
                    return
                plan["status"] = "paused"
                plan["pause_reason"] = f"题源会话过期：{exc}"
                plan["updated_at"] = _now()
                self._save()
                self.repo.add_event(
                    "source.auth_expired",
                    str(exc),
                    level="error",
                    actor="intake",
                )
                self.notifier.notify(title="solo-auto 需要重新登录", status="题源会话已过期")
                self.bus.publish("intake.updated", {"status": "paused"})
                return
            if candidate is None:
                if dispatcher is not None and dispatcher.v3_enabled:
                    dispatcher.register_hold(
                        source="source_guard",
                        scope=["intake"],
                        reason_code="source.empty",
                        reason="当前筛选下没有可领取或可复用的工程",
                        auto=True,
                    )
                    return
                plan["status"] = "paused"
                plan["pause_reason"] = "当前筛选下没有可领取或可复用的工程"
                plan["updated_at"] = _now()
                self._save()
                return
            if dispatcher is not None and dispatcher.v3_enabled and "source_guard" in dispatcher.holds.holds:
                dispatcher.release_hold("source_guard", actor="intake")
            engineering_id = str(_engineering_id(candidate))
            plan["attempted_engineering_ids"].append(engineering_id)
            claimed_meta: dict[str, Any] | None = None
            task_created = False
            try:
                claimed = await self.source.claim(str(plan["project_id"]), engineering_id)
                claim_id = str(claimed.get("claim_id") or engineering_id)
                meta = await self.staging.stage(
                    self.source,
                    str(plan["project_id"]),
                    engineering_id,
                    claimed,
                    claim_id,
                    project_name=str(plan.get("project_name") or plan["project_id"]),
                )
                claimed_meta = meta
                self.ledger.record_claim(meta)
                self.repo.upsert_claim(
                    {
                        "id": claim_id,
                        "project_id": str(plan["project_id"]),
                        "engineering_id": engineering_id,
                        "engineering_name": str(meta.get("name") or engineering_id),
                        "claimed_at": str(meta.get("downloadedAt") or _now()),
                        "released_at": None,
                        "task_id": None,
                        "zip_sha256": meta.get("zipSha256"),
                    }
                )
                task = await self.tasks.create(
                    staging_id=engineering_id,
                    task_name=str(meta.get("name") or engineering_id),
                )
                task_created = True
                task_id = str(task["task_id"])
                await self.autopilot.enroll([task_id])
                plan["task_ids"].append(task_id)
                plan["claimed_engineering_ids"].append(engineering_id)
                plan["updated_at"] = _now()
                self._save()
            except ApiError as exc:
                if claimed_meta is not None and not task_created:
                    await self._release_staged_claim(claimed_meta, "自动建题失败，已归还原工程")
                plan["failed_claims"].append({"engineering_id": engineering_id, "code": exc.code, "message": exc.message})
                plan["updated_at"] = _now()
                self._save()
                if exc.status_code == 401:
                    plan["status"] = "paused"
                    self._save()
            self.bus.publish("intake.updated", {"counts": self._counts(plan), "status": plan["status"]})

    @property
    def max_in_hand(self) -> int:
        dispatcher = getattr(self.autopilot, "dispatcher", None)
        if dispatcher is not None and dispatcher.v3_enabled:
            return int(dispatcher.effective_in_hand)
        if self.intake_settings.max_in_hand > 0:
            return self.intake_settings.max_in_hand
        return max(1, self.settings.autopilot.parallel_authoring + self.settings.execution.max_containers)

    async def _next_candidate(self, plan: dict[str, Any]) -> dict[str, Any] | None:
        filters = dict(plan.get("filters") or {})
        page = 1
        failed = {
            str(item.get("engineering_id") or "")
            for item in plan.get("failed_claims") or []
            if isinstance(item, dict)
        }
        active_staging = {
            str(item.get("engineeringId") or "")
            for item in self.staging.list()
            if str(item.get("projectId") or "") == str(plan.get("project_id"))
        }
        stats = {
            str(row["engineering_id"]): row
            for row in self.ledger.engineering_stats(project_id=str(plan["project_id"]))
        }
        excluded_tags = {str(tag) for tag in self.intake_settings.exclude_tags}
        candidates: list[dict[str, Any]] = []
        while page <= 20:
            response = await self.source.list_engineering(
                str(plan["project_id"]),
                q=str(filters.get("q") or ""),
                language=str(filters.get("language") or ""),
                min_files=_int_or_none(filters.get("min_files")),
                max_files=_int_or_none(filters.get("max_files")),
                tag_ids=[str(item) for item in filters.get("tag_ids") or []],
                scope="available",
                page=page,
                page_size=100,
            )
            items = [item for item in response.get("items") or [] if isinstance(item, dict)]
            for item in items:
                engineering_id = str(_engineering_id(item))
                if not engineering_id or engineering_id in failed or engineering_id in active_staging:
                    continue
                tags = {_tag_name(tag) for tag in item.get("tags") or []}
                if tags & excluded_tags:
                    continue
                usage = stats.get(engineering_id)
                if plan.get("exclude_delivered") and usage and int(usage.get("delivered") or 0) > 0:
                    continue
                max_uses = int(plan.get("max_uses") or 0)
                if max_uses and usage and int(usage.get("used") or 0) >= max_uses:
                    continue
                candidates.append(item)
            total = int(response.get("total") or 0)
            if page * int(response.get("page_size") or 100) >= total:
                break
            page += 1
        if not candidates:
            return None

        def priority(pair: tuple[int, dict[str, Any]]) -> tuple[int, int]:
            index, item = pair
            usage = stats.get(_engineering_id(item), {})
            return int(usage.get("used") or 0), index

        return min(
            enumerate(candidates),
            key=priority,
        )[1]

    def _counts(self, plan: dict[str, Any]) -> dict[str, int]:
        completed = active = failed = recoverable = orphaned = 0
        for task_id in plan.get("task_ids") or []:
            job = self.autopilot.job(str(task_id))
            if not job:
                task = self.repo.get_task(str(task_id))
                if task is None or task.get("job_status") == "aborted":
                    failed += 1
                    recoverable += 1
                elif task.get("job_status") == "archived":
                    if task.get("exported_at"):
                        completed += 1
                    else:
                        failed += 1
                else:
                    orphaned += 1
                    recoverable += 1
            elif job.get("stage") in {"done", "awaiting_audit"}:
                completed += 1
            elif job.get("active"):
                active += 1
            elif job.get("stage") in {"failed", "stopped"}:
                failed += 1
                recoverable += 1
        staged = sum(
            1
            for item in self.staging.list()
            if str(item.get("projectId") or "") == str(plan.get("project_id")) and not item.get("taskRoot")
        )
        return {
            "target": int(plan.get("target") or 0),
            "completed": completed,
            "active": active,
            "failed": failed,
            "recoverable": recoverable,
            "orphaned": orphaned,
            "claimed": len(plan.get("task_ids") or []) + len(plan.get("failed_claims") or []),
            "staged": staged,
            "in_hand": active + orphaned + staged,
        }

    def _in_hand(self, plan: dict[str, Any]) -> int:
        return self._counts(plan)["in_hand"]

    async def _repair_missing_jobs(self, plan: dict[str, Any]) -> list[str]:
        missing: list[str] = []
        for task_id in plan.get("task_ids") or []:
            task = self.repo.get_task(str(task_id))
            if not task or str(task.get("job_status") or "") in {"archived", "aborted"}:
                continue
            if self.autopilot.job(str(task_id)) is None:
                missing.append(str(task_id))
        if not missing:
            return []
        await self.autopilot.enroll(missing)
        self.repo.add_event(
            "intake.recovered",
            f"生产计划补齐 {len(missing)} 个未接入自动交付的任务",
            actor="intake",
            details={"task_ids": missing},
        )
        return missing

    def _pending_staged(self, plan: dict[str, Any]) -> list[dict[str, Any]]:
        blocked = {
            str(item.get("engineering_id") or "")
            for item in plan.get("failed_claims") or []
            if isinstance(item, dict)
        }
        return [
            item
            for item in self.staging.list()
            if str(item.get("projectId") or "") == str(plan.get("project_id"))
            and not item.get("taskRoot")
            and Path(str(item.get("sourcePath") or "")).is_dir()
            and str(item.get("engineeringId") or "") not in blocked
        ]

    async def _adopt_staged(self, plan: dict[str, Any], meta: dict[str, Any]) -> None:
        engineering_id = str(meta.get("engineeringId") or "")
        plan["attempted_engineering_ids"].append(engineering_id)
        try:
            task = await self.tasks.create(
                staging_id=engineering_id,
                task_name=str(meta.get("name") or engineering_id),
            )
            task_id = str(task["task_id"])
            await self.autopilot.enroll([task_id])
            plan["task_ids"].append(task_id)
            plan["claimed_engineering_ids"].append(engineering_id)
            plan["updated_at"] = _now()
            self._save()
            self.repo.add_event(
                "intake.staged_adopted",
                f"{meta.get('name') or engineering_id} 已从暂存材料接回生产计划",
                actor="intake",
                task_id=task_id,
            )
        except ApiError as exc:
            plan["failed_claims"].append(
                {
                    "engineering_id": engineering_id,
                    "code": exc.code,
                    "message": exc.message,
                }
            )
            plan["updated_at"] = _now()
            self._save()
            await self._release_staged_claim(meta, "暂存材料建题失败，已归还原工程")
        self.bus.publish("intake.updated", {"counts": self._counts(plan), "status": plan["status"]})

    async def _release_finished_claims(self, plan: dict[str, Any]) -> None:
        tasks = {
            str(task.get("task_root") or ""): task
            for task in self.repo.query("SELECT * FROM tasks")
        }
        for meta in self.staging.list():
            if str(meta.get("projectId") or "") != str(plan.get("project_id")) or not meta.get("taskRoot"):
                continue
            task = tasks.get(str(meta.get("taskRoot") or ""))
            if not task:
                continue
            job = self.autopilot.job(str(task["id"]))
            terminal_job = bool(job and job.get("stage") in {"done", "awaiting_audit", "failed", "stopped"})
            terminal_task = str(task.get("job_status") or "") in {"exported", "archived", "aborted"}
            if terminal_job or terminal_task:
                await self._release_staged_claim(meta, "任务已结束，已归还原工程供后续复题")

    async def _release_staged_claim(self, meta: dict[str, Any], event_message: str) -> None:
        try:
            await self.source.release(
                str(meta.get("projectId") or ""),
                str(meta.get("engineeringId") or ""),
                str(meta.get("claimId") or ""),
            )
        except ApiError as exc:
            if exc.status_code != 404:
                self.repo.add_event(
                    "claim.release_failed",
                    f"{meta.get('name') or meta.get('engineeringId')} 归还失败：{exc.message}",
                    level="warn",
                    actor="intake",
                )
                return
        except Exception as exc:
            self.repo.add_event(
                "claim.release_failed",
                f"{meta.get('name') or meta.get('engineeringId')} 归还失败：{exc}",
                level="warn",
                actor="intake",
            )
            return
        self.repo.execute(
            "UPDATE claims SET released_at = ? WHERE id = ?",
            (_now(), str(meta.get("claimId") or "")),
        )
        self.staging.remove(str(meta.get("engineeringId") or ""))
        self.repo.add_event(
            "claim.released",
            f"{meta.get('name') or meta.get('engineeringId')}：{event_message}",
            actor="intake",
        )

    def _require_plan(self) -> dict[str, Any]:
        if not self.plan:
            raise ApiError(404, "INTAKE_NOT_FOUND", "当前没有生产计划")
        return self.plan

    def _save(self) -> None:
        self.plan and self.plan.update({"updated_at": _now()})
        temporary = self.state_file.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps({"plan": self.plan}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.state_file)

    def _load(self) -> None:
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        plan = data.get("plan")
        if isinstance(plan, dict):
            if plan.get("status") == "running":
                plan["status"] = "paused"
                plan["pause_reason"] = "后端重启，等待人工继续"
            self.plan = plan


def _engineering_id(item: dict[str, Any]) -> str:
    return str(item.get("engineering_id") or item.get("id") or "")


def _tag_name(tag: Any) -> str:
    return str(tag.get("name") or tag.get("id") or "") if isinstance(tag, dict) else str(tag)


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value not in {None, ""} else None
    except (TypeError, ValueError):
        return None


def _now() -> str:
    return datetime.now().astimezone().replace(microsecond=0).isoformat()
