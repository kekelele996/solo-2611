"""Application service container and lifecycle."""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from solo_auto.auto_dispose import AutoDisposer
from solo_auto.config_watch import ConfigWatcher
from solo_auto.dispatch.facts import docker_info_ok
from solo_auto.dispatch.kernel import Dispatcher
from solo_auto.events.bus import EventBus
from solo_auto.gateway.skill import SkillGateway
from solo_auto.instance_lock import InstanceLock
from solo_auto.logging_setup import configure_logging
from solo_auto.pipeline.audit import AuditService
from solo_auto.pipeline.autopilot import Autopilot
from solo_auto.pipeline.batch import BatchService
from solo_auto.pipeline.export_service import ExportService
from solo_auto.pipeline.feedback_service import FeedbackService
from solo_auto.pipeline.intake import IntakeService
from solo_auto.pipeline.janitor import Janitor
from solo_auto.pipeline.notify import Notifier
from solo_auto.pipeline.preflight import Preflight
from solo_auto.pipeline.prompt_service import PromptService
from solo_auto.pipeline.reconcile import Reconciler
from solo_auto.pipeline.recording_service import RecordingService
from solo_auto.pipeline.scheduler import RunScheduler
from solo_auto.pipeline.task_service import TaskService
from solo_auto.pipeline.trash import TrashService
from solo_auto.pipeline.usage import UsageLedger
from solo_auto.secrets import SecretStore
from solo_auto.settings import Paths, Settings, app_paths, load_settings, save_settings
from solo_auto.sources.project_hub import ProjectHubClient
from solo_auto.sources.solo2 import Solo2Client
from solo_auto.sources.staging import StagingManager
from solo_auto.spec_loader import load_spec
from solo_auto.store.repo import Repository
from solo_auto.store.state_watch import StateWatcher

LOGGER = logging.getLogger(__name__)


class AppContext:
    def __init__(self, home: Path | None = None, *, auto_start: bool = True) -> None:
        self.auto_start = auto_start
        self.paths: Paths = app_paths(home)
        self.settings: Settings = load_settings(self.paths)
        if not self.paths.config.exists():
            save_settings(self.paths, self.settings)
        configure_logging(self.paths.logs)
        self.secrets = SecretStore(self.paths.home)
        if not self.settings.notify.webhook_url:
            self.settings.notify.webhook_url = self.secrets.get("notify.webhook") or ""
        self.spec = load_spec(self.settings.skill_root)
        self.gateway = SkillGateway(self.settings.skill_root, self.paths.skill_logs)
        self.repo = Repository(self.paths.database)
        self.bus = EventBus()
        self.dispatcher = Dispatcher(
            paths=self.paths,
            settings=self.settings,
            repo=self.repo,
            gateway=self.gateway,
            bus=self.bus,
        )
        if self.settings._dispatch_migrated:
            self.settings._dispatch_migrated = False
            save_settings(self.paths, self.settings)
            self.repo.add_event(
                "dispatch.migrated",
                "已生成 v3 [dispatch] 配置，原有 v2 配置键保持不变",
                actor="system",
            )
        elif self.settings._dispatch_validation_error:
            self.repo.add_event(
                "dispatch.config_recovered",
                "dispatch 配置校验失败，已回退默认值",
                level="warn",
                actor="system",
                details={"error": self.settings._dispatch_validation_error},
            )
        self.ledger = UsageLedger(self.repo, self.settings.source.base_url)
        self.notifier = Notifier(self.settings.notify)
        self.instance_lock = InstanceLock(self.paths.instance_lock, self.paths.pid)
        self.reconciler = Reconciler(
            self.repo,
            self.paths,
            self.settings.tasks_dir,
            gateway=self.gateway,
        )
        self.staging = StagingManager(self.paths)
        self.source = ProjectHubClient(self.settings.source, self.secrets)
        self.solo2 = Solo2Client(self.settings.solo2, self.secrets)
        self.watcher = StateWatcher(self.settings.tasks_dir)
        self.scheduler = RunScheduler(
            paths=self.paths,
            gateway=self.gateway,
            repo=self.repo,
            reconciler=self.reconciler,
            bus=self.bus,
            dispatcher=self.dispatcher,
            attempt_total=self.settings.execution.attempts,
            run_mode=self.settings.execution.run_mode,
        )
        self.tasks = TaskService(
            self.settings,
            self.paths,
            self.gateway,
            self.repo,
            self.staging,
            self.reconciler,
            self.bus,
        )
        self.tasks.attach_ledger(self.ledger)
        self.prompts = PromptService(
            self.paths,
            self.gateway,
            self.repo,
            self.reconciler,
            self.bus,
            self.spec,
        )
        self.feedback = FeedbackService(
            self.paths,
            self.gateway,
            self.repo,
            self.reconciler,
            self.bus,
            self.spec,
        )
        self.exports = ExportService(
            self.paths,
            self.gateway,
            self.repo,
            self.reconciler,
            self.bus,
        )
        self.batch = BatchService(
            paths=self.paths,
            repo=self.repo,
            exports=self.exports,
            ledger=self.ledger,
            bus=self.bus,
        )
        self.recording = RecordingService(
            paths=self.paths,
            settings=self.settings.recording,
            gateway=self.gateway,
            repo=self.repo,
            reconciler=self.reconciler,
            bus=self.bus,
        )
        self.autopilot = Autopilot(
            paths=self.paths,
            skill_root=self.settings.skill_root,
            repo=self.repo,
            reconciler=self.reconciler,
            bus=self.bus,
            scheduler=self.scheduler,
            feedback=self.feedback,
            exports=self.exports,
            settings=self.settings.autopilot,
            gateway=self.gateway,
            dispatcher=self.dispatcher,
        )
        self.autopilot.attach_notifier(self.notifier)
        self.autopilot.breaker.attach_notifier(self.notifier)
        self.audits = AuditService(
            paths=self.paths,
            repo=self.repo,
            bus=self.bus,
            settings=self.settings.audit,
            breaker=self.autopilot.breaker,
            prompts=self.prompts,
            feedback=self.feedback,
            recording=self.recording,
        )
        self.audits.attach_autopilot(self.autopilot)
        self.autopilot.attach_audit(self.audits)
        self.autopilot.attach_ledger(self.ledger)
        self.trash = TrashService(
            paths=self.paths,
            repo=self.repo,
            bus=self.bus,
            settings=self.settings.trash,
        )
        self.janitor = Janitor(
            paths=self.paths,
            repo=self.repo,
            reconciler=self.reconciler,
            trash=self.trash,
            settings=self.settings.slim,
            bus=self.bus,
        )
        self.prompt_templates = self.autopilot.prompt_templates
        self.preflight = Preflight(
            paths=self.paths,
            settings=self.settings,
            gateway=self.gateway,
            autopilot=self.autopilot,
            templates=self.prompt_templates,
            source=self.source,
            secrets=self.secrets,
        )
        self.intake = IntakeService(
            paths=self.paths,
            settings=self.settings,
            repo=self.repo,
            source=self.source,
            staging=self.staging,
            tasks=self.tasks,
            autopilot=self.autopilot,
            ledger=self.ledger,
            bus=self.bus,
            notifier=self.notifier,
        )
        self.scheduler.attach_recovery(intake=self.intake, ledger=self.ledger)
        self.dispatcher.attach_runtime(
            autopilot=self.autopilot,
            preflight=self.preflight,
            source=self.source,
            recording=self.recording,
        )
        self.disposer = AutoDisposer(self)
        self.started_at = datetime.now().astimezone()
        self.skill_version = ""
        self.source_online = False
        self._background: list[asyncio.Task[Any]] = []
        self._started = False
        self.watcher_started = False
        self.config_watcher = ConfigWatcher(self)

    async def startup(self) -> None:
        if self._started:
            return
        self.instance_lock.acquire()
        self.skill_version = await self.gateway.version()
        await self.reconciler.run()
        self.ledger.backfill()
        if self.auto_start:
            loop = asyncio.get_running_loop()
            self.watcher.start(loop)
            self.watcher_started = True
            self._background = [
                asyncio.create_task(self.reconciler.loop()),
                asyncio.create_task(self.watcher.changes(self.reconciler.run)),
                asyncio.create_task(self._source_loop()),
                asyncio.create_task(self._cleanup_loop()),
                asyncio.create_task(self.disposer.loop()),
            ]
            await self.dispatcher.start()
            await self.scheduler.start()
            await self.autopilot.start()
            await self.janitor.start()
            await self.intake.start()
            self.config_watcher.start()
        self.repo.add_event(
            "system.started",
            f"solo-auto 启动，技能版本 {self.skill_version}",
            actor="system",
        )
        self._started = True

    async def shutdown(self) -> None:
        if not self._started:
            return
        await self.config_watcher.stop()
        await self.autopilot.stop()
        await self.intake.shutdown()
        await self.janitor.stop()
        await self.notifier.close()
        await self.scheduler.stop()
        await self.dispatcher.stop()
        for task in self._background:
            task.cancel()
        await asyncio.gather(*self._background, return_exceptions=True)
        if self.watcher_started:
            self.watcher.stop()
            self.watcher_started = False
        await self.source.close()
        self.repo.close()
        self.instance_lock.release()
        self._started = False

    async def _source_loop(self) -> None:
        while True:
            was_online = self.source_online
            try:
                await self.source.list_projects()
                self.source_online = True
            except Exception:
                self.source_online = False
            if was_online and not self.source_online:
                self.notifier.notify(title="solo-auto 题源离线", status="题源会话已过期或服务不可达")
            self.bus.publish(
                "system",
                {
                    "docker_ok": await self.docker_ok(),
                    "source_online": self.source_online,
                    "offline": not self.source_online,
                },
            )
            await asyncio.sleep(15)

    async def _cleanup_loop(self) -> None:
        while True:
            try:
                self.repo.prune_events(self.settings.storage.event_retention_days)
                cutoff = (
                    datetime.now().timestamp()
                    - self.settings.storage.skill_log_retention_days * 86400
                )
                for path in self.paths.skill_logs.rglob("*.log"):
                    if path.stat().st_mtime < cutoff:
                        path.unlink(missing_ok=True)
            except Exception:
                LOGGER.exception("retention cleanup failed")
            await asyncio.sleep(3600)

    async def system_status(self) -> dict[str, Any]:
        if self.dispatcher.v3_enabled:
            snapshot = self.dispatcher.snapshot()
            raw_pools = snapshot.get("pools")
            pools = raw_pools if isinstance(raw_pools, dict) else {}
            raw_containers = pools.get("containers")
            containers = raw_containers if isinstance(raw_containers, dict) else {}
            raw_sessions = pools.get("sessions")
            sessions = raw_sessions if isinstance(raw_sessions, dict) else {}
            facts = await self.dispatcher.facts.snapshot()
            raw_counts = snapshot.get("counts")
            counts = raw_counts if isinstance(raw_counts, dict) else {}
            return {
                "docker_ok": facts.docker.healthy,
                "source_online": self.source_online,
                "offline": not self.source_online,
                "disk_free_gb": round(facts.disk_free_gb, 1),
                "disk_low": facts.disk_free_gb < self.settings.dispatch.disk.floor_gb,
                "running": int(containers.get("used") or 0),
                "queued": int(counts.get("waiting") or 0),
                "blocked": int(counts.get("needs_action") or 0),
                "auth_blocked": 1 if "breaker" in self.dispatcher.holds.holds else 0,
                "skill_version": self.skill_version,
                "dispatch_effective_state": snapshot.get("effective_state", "unknown"),
                "dispatch_sentence": snapshot.get("sentence", ""),
                "containers_used": int(containers.get("used") or 0),
                "containers_limit": int(containers.get("limit") or 0),
                "sessions_used": int(sessions.get("used") or 0),
                "sessions_limit": int(sessions.get("limit") or 0),
                "holds": len(self.dispatcher.holds.active()),
            }
        disk = shutil.disk_usage(self.paths.home)
        low_disk = disk.free / 1024**3 < self.settings.storage.disk_floor_gb
        auth_row = self.repo.query_one(
            """
            SELECT COUNT(DISTINCT events.task_id) AS count
            FROM events JOIN tasks ON tasks.id = events.task_id
            WHERE tasks.job_status = 'blocked'
              AND tasks.skill_status = 'run_failed'
              AND (
                events.action = 'run.auth_breaker'
                OR (events.action = 'run.failed' AND events.details LIKE '%"kind": "auth"%')
              )
            """
        ) or {"count": 0}
        running_row = self.repo.query_one("SELECT COUNT(*) AS count FROM tasks WHERE job_status = 'running'") or {
            "count": 0
        }
        return {
            "docker_ok": await self.docker_ok(),
            "source_online": self.source_online,
            "offline": not self.source_online,
            "disk_free_gb": round(disk.free / 1024**3, 1),
            "disk_low": low_disk,
            "running": max(len(self.scheduler.active), int(running_row.get("count") or 0)),
            "queued": len(self.scheduler.queue),
            "blocked": int(
                (self.repo.query_one("SELECT COUNT(*) n FROM tasks WHERE job_status='blocked'") or {"n": 0})["n"]
            ),
            "auth_blocked": int(auth_row.get("count") or 0),
            "skill_version": self.skill_version,
        }

    async def docker_ok(self) -> bool:
        return await docker_info_ok()

    def git_sha(self) -> str:
        try:
            proc = subprocess.run(
                ["git", "-C", str(Path(__file__).resolve().parents[2]), "rev-parse", "--short", "HEAD"],
                capture_output=True,
                text=True,
                check=False,
            )
            return proc.stdout.strip() if proc.returncode == 0 else "unknown"
        except OSError:
            return "unknown"

    def save_settings(self) -> None:
        save_settings(self.paths, self.settings)
