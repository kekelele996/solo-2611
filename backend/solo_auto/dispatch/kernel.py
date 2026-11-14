"""Unified v3 dispatcher.

The existing v2 autopilot remains the stage executor. This kernel owns the
admission decision, resource accounting, scoped holds, persisted queue, probes,
retry policy, and the explanation shown by the scheduling console.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from contextlib import suppress
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from solo_auto.dispatch.classify import Evidence, Failure, FailureClassifier
from solo_auto.dispatch.clock import Clock, ClockState, observe_clock
from solo_auto.dispatch.facts import FactsCollector, FactsSnapshot
from solo_auto.dispatch.faults import FaultController
from solo_auto.dispatch.gates import (
    LAUNCH_SPACING_SECONDS,
    GateDecision,
    evaluate_agent_gates,
    evaluate_container_gates,
    evaluate_creation_gates,
    evaluate_session_gates,
)
from solo_auto.dispatch.guards import GuardRunner
from solo_auto.dispatch.holds import Hold, HoldRegistry
from solo_auto.dispatch.intents import IntentLog
from solo_auto.dispatch.policy import PolicyDecision, RecoveryPolicy
from solo_auto.dispatch.pools import PoolBook
from solo_auto.dispatch.probes import ProbeManager, ProbeResult
from solo_auto.dispatch.queue import IN_HAND, DispatchItem, DispatchQueue
from solo_auto.dispatch.recovery import DispatchRecovery, RecoveryResult
from solo_auto.dispatch.storage import read_json, write_json
from solo_auto.dispatch.throughput import Outcome, ThroughputLedger
from solo_auto.dispatch.watchdog import LoopHealth
from solo_auto.events.bus import EventBus
from solo_auto.gateway.skill import SkillGateway
from solo_auto.settings import DispatchSettings, Paths, Settings, save_settings
from solo_auto.store.repo import Repository

LOGGER = logging.getLogger(__name__)
# 容器优先时不限在手任务数，用设置允许的最大值占位；会话最多 8 个。
MAX_IN_HAND = 50
MAX_SESSIONS = 8


class RuntimeProtocol(Protocol):
    async def retry(self, task_id: str, *, from_stage: str = "") -> dict[str, Any]: ...


class PreflightProtocol(Protocol):
    async def run(self) -> dict[str, Any]: ...

    async def probe_api(self) -> dict[str, Any]: ...


class Dispatcher:
    def __init__(
        self,
        *,
        paths: Paths,
        settings: Settings,
        repo: Repository,
        gateway: SkillGateway,
        bus: EventBus,
        clock: Clock | None = None,
        faults: FaultController | None = None,
    ) -> None:
        self.paths = paths
        self.settings = settings
        self.config: DispatchSettings = settings.dispatch
        self.repo = repo
        self.gateway = gateway
        self.bus = bus
        self.clock = clock or Clock()
        self.faults = faults or FaultController()
        self.facts = FactsCollector(
            paths.home,
            timeout_seconds=self.config.docker_timeout_seconds,
            stale_seconds=self.config.docker_stale_seconds,
            faults=self.faults,
        )
        self.holds = HoldRegistry(paths.run / "holds.json")
        self.queue = DispatchQueue(repo)
        self.pools_book = PoolBook()
        self.classifier = FailureClassifier(paths.home)
        self.policy = RecoveryPolicy(self.config.retry)
        self.recovery = DispatchRecovery(self.queue)
        self.intents = IntentLog(repo)
        self.throughput = ThroughputLedger(repo)
        self.health = LoopHealth()
        self.probes = ProbeManager(holds=self.holds, facts=self.facts, settings=self.config)
        self.guards = GuardRunner(self.queue, self.config)
        self.state_file = paths.run / "dispatch.json"
        self.starts_file = paths.run / "starts.json"
        self.state = self._load_state()
        self.desired_state = str(self.state.get("desired_state") or ("running" if self.config.enabled else "paused"))
        raw_plan = self.state.get("plan")
        self.plan: dict[str, Any] = raw_plan if isinstance(raw_plan, dict) else {}
        self.started_monotonic = self.clock.monotonic()
        self.last_launch_at = 0.0
        self.last_session_at = 0.0
        # 建题冷却必须跨重启生效，否则每次重启都会立刻连建一题。
        try:
            self.last_created_at = float(self.state.get("last_created_at") or 0.0)
        except (TypeError, ValueError):
            self.last_created_at = 0.0
        self.limit_resync_until = ""
        self.skill_limit = 0
        self.limit_synced = False
        self._clock_state: ClockState | None = None
        self._fact_snapshot: FactsSnapshot | None = None
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._stopping = False
        self._event_signatures: dict[str, str] = {}
        self._last_capacity_sample = 0.0
        self._last_limit_sync = 0.0
        self._probe_next_at = self.clock.monotonic() + self.config.llm_guard.probe_seconds
        self._probe_next_docker = self.clock.monotonic()
        self._start_id = ""
        self.autopilot: RuntimeProtocol | None = None
        self.preflight: PreflightProtocol | None = None
        self.source: Any = None
        self.recording: Any = None
        self._migrate_queue()
        self._configure_pools()

    @property
    def v3_enabled(self) -> bool:
        return self.config.engine == "v3"

    def attach_runtime(
        self,
        *,
        autopilot: RuntimeProtocol,
        preflight: PreflightProtocol,
        source: Any = None,
        recording: Any = None,
    ) -> None:
        self.autopilot = autopilot
        self.preflight = preflight
        self.source = source
        self.recording = recording
        self.probes.llm_probe = preflight.probe_api
        if source is not None:
            self.probes.source_probe = source.test
        if recording is not None:
            self.probes.recording_probe = self._recording_probe
        self.probes.session_probe = self._session_probe
        self.probes.skill_probe = self._skill_probe

    async def start(self) -> None:
        if not self.v3_enabled:
            return
        self._record_start()
        recent_crashes = self._recent_unclean_starts()
        if recent_crashes >= 2:
            # 只记日志：异常退出后照常恢复调度，不再挂起等人处理。
            self.repo.add_event(
                "dispatch.crash_loop",
                f"15 分钟内异常退出 {recent_crashes} 次，已照常恢复调度",
                level="warn",
                actor="dispatch",
            )
        # 旧版本留下的人工挂起（崩溃挂起）直接丢弃。
        self.holds.release("crash_loop", actor="dispatch")
        self._close_legacy_quarantine()
        await self.tick()
        await self.sync_skill_limit(sync=False)
        definitions = {
            "facts": (2.0, 60.0, self._facts_loop),
            "gate": (2.0, 60.0, self._gate_loop),
            "probe": (5.0, 120.0, self._probe_loop),
            "retry": (3.0, 60.0, self._retry_loop),
        }
        for name, (interval, stall, runner) in definitions.items():
            self.health.register(name, interval=interval, stall_after=stall)
            task = asyncio.create_task(runner(name, interval))
            self._tasks[name] = task
            self.health.track(name, task)
        self.repo.add_event(
            "dispatch.started",
            f"v3 调度中心已启动，容器上限 {self.effective_container_limit}",
            actor="dispatch",
        )
        self.bus.publish("dispatch.limit", {"effective": self.effective_container_limit, "changed": {}})
        self._publish()

    async def stop(self) -> None:
        self._stopping = True
        self._record_clean_exit()
        for task in self._tasks.values():
            task.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()

    async def tick(self) -> None:
        snapshot = await self.facts.snapshot(force=True)
        self._fact_snapshot = snapshot
        self.pools_book.occupy_from_facts(snapshot)
        self._sync_runtime_pools()
        self._configure_pools()
        self._check_clock()
        if snapshot.disk_free_gb < self.config.disk.floor_gb:
            await self.probes.probe("disk_guard")
        elif "disk_guard" in self.holds.holds and snapshot.disk_free_gb >= (
            self.config.disk.floor_gb * self.config.disk.resume_factor
        ):
            self._release_hold("disk_guard", actor="probe")
        await self._refresh_waits()

    async def sync_skill_limit(self, *, sync: bool = True) -> dict[str, Any]:
        desired = self.config.containers
        requested = self.config.containers
        result = await self.gateway.call(["config", "show"], timeout=30)
        actual = 0
        if result.code == 0 and result.data:
            claude = result.data.get("claude")
            if isinstance(claude, dict):
                try:
                    actual = int(claude.get("maxContainers") or 0)
                except (TypeError, ValueError):
                    actual = 0
        self.skill_limit = actual
        self.limit_synced = actual == requested
        if actual != requested:
            self.repo.add_event(
                "limit.drift",
                f"技能容器上限 {actual or '未知'} 与平台 {requested} 不一致",
                level="warn",
                actor="dispatch",
                details={"actual": actual, "requested": requested},
            )
            self.limit_resync_until = (datetime.now(UTC) + timedelta(seconds=20)).replace(microsecond=0).isoformat()
            if sync:
                updated = await self.gateway.call(["config", "set", f"claude.maxContainers={requested}"], timeout=30)
                if updated.code == 0:
                    actual = requested
                    self.skill_limit = actual
                    self.limit_synced = True
        return {
            "requested": requested,
            "actual": actual,
            "synced": self.limit_synced,
            "ceiling": max(requested, desired),
        }

    async def request_container(self, task_id: str) -> GateDecision:
        item = self.queue.get(task_id) or self.queue.enqueue(task_id)
        snapshot = await self.facts.snapshot()
        self.pools_book.occupy_from_facts(snapshot)
        self._configure_pools()
        decision = evaluate_container_gates(
            item,
            settings=self.config,
            desired_state=self.desired_state,
            facts=snapshot,
            holds=self.holds,
            pools=self.pools_book.snapshot(),
            startup_elapsed=self.clock.monotonic() - self.started_monotonic,
            last_launch_at=self.last_launch_at,
            limit_resync_until=self.limit_resync_until,
            now=self.clock.wall(),
        )
        if decision.allowed:
            self.pools_book.reserve_container(task_id)
            self.last_launch_at = self.clock.wall().timestamp()
            self.queue.update(
                task_id,
                status="launching",
                stage=item.stage or "running",
                wait_code="",
                wait_text="",
                wait_until="",
            )
            self._event_once(f"gate-pass:{task_id}", "gate.passed", f"{task_id} 通过容器准入", task_id=task_id)
        else:
            self.queue.update(
                task_id,
                wait_code=decision.code,
                wait_text=decision.text,
                wait_until=decision.until,
            )
            self._event_once(
                f"gate-wait:{task_id}:{decision.code}",
                "gate.waiting",
                decision.text,
                task_id=task_id,
                details={"code": decision.code, "gate": decision.gate},
            )
        self._publish()
        return decision

    def container_demand(self, *, extra: int = 0) -> dict[str, Any]:
        """容器占用 = 运行中的受管容器 + 在手任务里还没拿到容器、但仍需要容器的数量。

        沿用 sg-auto 的容器优先口径：刚建好还没起容器的题按 1 计，跑完容器阶段（ran 之后）计 0。
        ``running`` 却长时间没有容器、状态文件也不再更新的题视为执行器已退出，不再计入，
        避免一个失联任务把名额一直占着。``extra`` 用于计入已领取、尚未建题的暂存工程。
        """
        snapshot = self._fact_snapshot
        live = set(snapshot.docker.containers) if snapshot is not None else set()
        waiting: list[str] = []
        now = self.clock.wall().timestamp()
        for task_id in self._in_hand_task_ids():
            task = self.repo.get_task(task_id) or {}
            root = str(task.get("task_root") or "")
            if not root:
                waiting.append(task_id)
                continue
            status, container, mtime = _task_run_state(Path(root))
            if status not in NEEDS_CONTAINER:
                continue
            if container and container in live:
                continue
            if status == "running" and mtime and now - mtime > self.config.phantom_seconds:
                continue
            waiting.append(task_id)
        used = len(live) + len(waiting) + max(0, extra)
        return {
            "live": len(live),
            "waiting": len(waiting),
            "extra": max(0, extra),
            "used": used,
            "limit": self.config.containers,
            "waiting_task_ids": waiting,
        }

    def creation_decision(self, *, in_hand: int, extra_demand: int = 0) -> GateDecision:
        """领题建题前调用：只回答“现在能不能再建一题”，不改任何状态。"""
        snapshot = self._fact_snapshot
        if snapshot is None:
            snapshot = FactsSnapshot(docker=self.facts._docker, disk_free_gb=self.config.disk.floor_gb, observed_at=0)
        demand = self.container_demand(extra=extra_demand)
        return evaluate_creation_gates(
            settings=self.config,
            desired_state=self.desired_state,
            facts=snapshot,
            holds=self.holds,
            startup_elapsed=self.clock.monotonic() - self.started_monotonic,
            in_hand=in_hand,
            container_used=int(demand["used"]),
            last_created_at=self.last_created_at,
            now=self.clock.wall(),
        )

    def mark_task_created(self, *, engineering_id: str = "", actor: str = "intake") -> None:
        """记录一次建题尝试；冷却从这一刻起算，领取失败也不提前结束冷却。"""
        self.last_created_at = self.clock.wall().timestamp()
        self._save_state()
        self.repo.add_event(
            "dispatch.task_created",
            f"开始建题{('：' + engineering_id) if engineering_id else ''}，"
            f"{self.config.create_cooldown_seconds} 秒内不再建新题",
            actor=actor,
            details={"engineering_id": engineering_id, "cooldown": self.config.create_cooldown_seconds},
        )
        self._publish()

    def _in_hand_task_ids(self) -> list[str]:
        jobs = getattr(self.autopilot, "jobs", None) if self.autopilot is not None else None
        if isinstance(jobs, dict) and jobs:
            ids: list[str] = []
            for task_id, job in jobs.items():
                if str(getattr(job, "stage", "")) not in {"pending", "chatgpt", "authoring", "running"}:
                    continue
                if getattr(job, "desktop_running", False) and getattr(job, "awaiting_platform", False):
                    continue
                ids.append(str(task_id))
            return ids
        return [item.task_id for item in self.queue.all_items(include_terminal=False) if item.status in IN_HAND]

    def creation_view(self, *, in_hand: int | None = None, extra_demand: int = 0) -> dict[str, Any]:
        count = self.in_hand_count() if in_hand is None else in_hand
        decision = self.creation_decision(in_hand=count, extra_demand=extra_demand)
        cooldown = max(20, self.config.create_cooldown_seconds)
        next_at = ""
        if self.last_created_at > 0:
            next_at = (
                datetime.fromtimestamp(self.last_created_at + cooldown, UTC).replace(microsecond=0).isoformat()
            )
        return {
            "allowed": decision.allowed,
            "code": decision.code,
            "text": decision.text or "可以建下一题",
            "until": decision.until,
            "last_created_at": (
                datetime.fromtimestamp(self.last_created_at, UTC).replace(microsecond=0).isoformat()
                if self.last_created_at > 0
                else ""
            ),
            "next_allowed_at": next_at,
            "cooldown_seconds": cooldown,
            "in_hand": count,
            "mode": self.config.mode,
            "max_tasks": self.config.max_tasks,
            "containers": self.container_demand(extra=extra_demand),
        }

    def mark_container_started(self, task_id: str) -> None:
        self.queue.update(task_id, status="active", stage="running", wait_code="", wait_text="", wait_until="")
        self._publish()

    def release_container(self, task_id: str, *, success: bool = False) -> None:
        self.pools_book.release_container(task_id)
        if success:
            self.queue.update(task_id, status="active", stage="running", wait_code="", wait_text="", wait_until="")
        self._publish()

    async def request_session(self, task_id: str, *, stage: str = "chatgpt") -> GateDecision:
        item = self.queue.get(task_id) or self.queue.enqueue(task_id, stage=stage)
        decision = evaluate_session_gates(
            item,
            settings=self.config,
            desired_state=self.desired_state,
            holds=self.holds,
            pools=self.pools_book.snapshot(),
            startup_elapsed=self.clock.monotonic() - self.started_monotonic,
            last_session_at=self.last_session_at,
            now=self.clock.wall(),
        )
        if decision.allowed:
            key = f"task:{task_id}"
            self.pools_book.acquire("sessions", key)
            self.last_session_at = self.clock.wall().timestamp()
            self.queue.update(task_id, status="active", stage=stage)
        else:
            self.queue.update(task_id, wait_code=decision.code, wait_text=decision.text, wait_until=decision.until)
            self._event_once(
                f"session-wait:{task_id}:{decision.code}",
                "gate.waiting",
                decision.text,
                task_id=task_id,
                details={"code": decision.code, "scope": "sessions"},
            )
        self._publish()
        return decision

    def release_session(self, task_id: str) -> None:
        self.pools_book.release("sessions", f"task:{task_id}")
        self._publish()

    def request_agent(self, task_id: str, *, stage: str) -> GateDecision:
        item = self.queue.get(task_id) or self.queue.enqueue(task_id, stage=stage)
        pool = "authoring" if stage == "authoring" else "delivering"
        decision = evaluate_agent_gates(
            item,
            settings=self.config,
            desired_state=self.desired_state,
            holds=self.holds,
            pools=self.pools_book.snapshot(),
            pool_name=pool,
            now=self.clock.wall(),
        )
        if decision.allowed:
            self.pools_book.acquire(pool, f"task:{task_id}")
            self.queue.update(task_id, status="active", stage=stage)
        else:
            self.queue.update(task_id, wait_code=decision.code, wait_text=decision.text, wait_until=decision.until)
        return decision

    def release_agent(self, task_id: str, *, stage: str) -> None:
        pool = "authoring" if stage == "authoring" else "delivering"
        self.pools_book.release(pool, f"task:{task_id}")

    async def set_desired_state(self, desired: str, *, actor: str = "user") -> dict[str, Any]:
        if desired not in {"running", "paused", "draining"}:
            raise ValueError("desired state must be running, paused, or draining")
        previous = self.desired_state
        self.desired_state = desired
        self._save_state()
        self.repo.add_event(
            "dispatch.state_changed",
            f"调度意图从 {previous} 改为 {desired}",
            actor=actor,
            details={"from": previous, "to": desired},
        )
        self._publish()
        return self.snapshot()

    def _install_config(self, config: DispatchSettings) -> None:
        """Validate and install a dispatch config into every component that reads it."""
        # Re-run model validation and enforce cross-field constraints.
        validated = DispatchSettings.model_validate(config.model_dump())
        self.settings.dispatch = validated
        self.config = validated
        self.policy.settings = self.config.retry
        self.guards.settings = self.config
        self.probes.settings = self.config
        self.facts.timeout_seconds = self.config.docker_timeout_seconds
        self.facts.stale_seconds = self.config.docker_stale_seconds
        self._configure_pools()
        self._save_state()

    async def apply_config(self, config: DispatchSettings, *, actor: str = "config") -> dict[str, Any]:
        """Hot-apply a dispatch config read from disk; running work is never interrupted.

        Raising limits lets the next gate pass start more work at the usual
        spacing. Lowering them only stops new launches, so the pool drains down
        to the new limit as running tasks finish.
        """
        before = self.config.model_dump()
        old_containers = self.effective_container_limit
        self._install_config(config)
        after = self.config.model_dump()
        changed = _diff(before, after)
        if not changed:
            return {"changed": {}}
        sync: dict[str, Any] = {}
        if self.effective_container_limit != old_containers:
            sync = await self.sync_skill_limit(sync=True)
        self.repo.add_event(
            "dispatch.limits_changed",
            "调度配置已热更新：" + "，".join(f"{key} {old}→{new}" for key, (old, new) in list(changed.items())[:8]),
            actor=actor,
            details={"changed": changed, "sync": sync},
        )
        self.bus.publish("dispatch.limit", {"effective": self.effective_container_limit, "changed": changed})
        self._publish()
        return {"changed": changed, "sync": sync}

    async def update_limits(self, values: dict[str, Any], *, actor: str = "user") -> dict[str, Any]:
        allowed = {"mode", "containers", "max_tasks", "create_cooldown_seconds", "keep_awake"}
        changed: dict[str, list[Any]] = {}
        for key, value in values.items():
            if key not in allowed or value is None:
                continue
            old = getattr(self.config, key)
            if old != value:
                changed[key] = [old, value]
                setattr(self.config, key, value)
        self._install_config(self.config)
        save_settings(self.paths, self.settings)
        sync = await self.sync_skill_limit(sync=True)
        self.repo.add_event(
            "dispatch.limits_changed",
            "调度上限已更新",
            actor=actor,
            details={"changed": changed, "sync": sync},
        )
        self.bus.publish("dispatch.limit", {"effective": self.effective_container_limit, "changed": changed})
        self._publish()
        return {"snapshot": self.snapshot(), "changed": changed, "sync": sync}

    async def update_policies(self, values: dict[str, Any], *, actor: str = "user") -> dict[str, Any]:
        tables = {"guard": self.config.guard, "retry": self.config.retry, "llm_guard": self.config.llm_guard, "disk": self.config.disk, "watchdog": self.config.watchdog, "window": self.config.window}
        changed: dict[str, list[Any]] = {}
        for table_name, model in tables.items():
            patch = values.get(table_name)
            if not isinstance(patch, dict):
                continue
            for key, value in patch.items():
                if not hasattr(model, key):
                    continue
                old = getattr(model, key)
                if old != value:
                    setattr(model, key, value)
                    changed[f"{table_name}.{key}"] = [old, value]
        validated = DispatchSettings.model_validate(self.config.model_dump())
        self.settings.dispatch = validated
        self.config = validated
        self.policy.settings = self.config.retry
        self.guards.settings = self.config
        self.probes.settings = self.config
        save_settings(self.paths, self.settings)
        self.repo.add_event("dispatch.policies_changed", "调度策略已更新", actor=actor, details={"changed": changed})
        self._publish()
        return {"snapshot": self.snapshot(), "changed": changed}

    async def on_failure(
        self,
        task_id: str,
        evidence: Evidence,
        *,
        actor: str = "executor",
        final: bool = False,
    ) -> tuple[Failure, PolicyDecision, DispatchItem | None]:
        """记录一次失败：能重跑的排到队尾，会话已消耗的（``final``）直接结束、由自动清理补新题。"""
        item = self.queue.get(task_id) or self.queue.enqueue(task_id, stage=evidence.stage)
        failure = self.classifier.classify(evidence)
        if failure.kind.startswith("gateway.") and self.preflight is not None:
            asyncio.create_task(self.probes.probe("llm_guard"))
        if failure.kind.startswith("docker.") and self.preflight is not None:
            asyncio.create_task(self.probes.probe("docker_guard"))
        if final and not failure.hold_source:
            failure = Failure(
                kind=failure.kind,
                transient=False,
                charge=False,
                detail=failure.detail,
                matched_by=f"{failure.matched_by};final",
                evidence=failure.evidence,
                disposition="final",
            )
        decision, updated = self.policy.apply(self.queue, item, failure)
        if decision.hold_source:
            self.holds.register(
                source=decision.hold_source,
                scope=decision.hold_scope or ["global"],
                reason_code=failure.kind,
                reason=failure.detail,
                probe_seconds=self.config.llm_guard.paused_probe_seconds,
                self_heal_seconds=3600,
                caused_by_task=task_id,
                metadata={"error": failure.detail, "matched_by": failure.matched_by},
            )
        event_level = "error" if decision.action in {"retry", "give_up"} else "info"
        self.repo.add_event(
            "retry.decided" if decision.action == "retry" else f"retry.{decision.action}",
            f"{task_id} 分类为 {failure.kind}：{decision.detail[:240]}",
            level=event_level,
            actor=actor,
            task_id=task_id,
            details={
                "failure": failure.view(),
                "decision": asdict(decision),
                "matched_by": failure.matched_by,
            },
        )
        self._publish()
        return failure, decision, updated

    def on_success(self, task_id: str, *, stage: str = "exporting") -> None:
        item = self.queue.get(task_id)
        if item is None:
            return
        attempts = dict(item.attempts)
        attempts.pop(stage, None)
        self.queue.update(
            task_id,
            attempts=attempts,
            repeat_signature="",
            wait_code="",
            wait_text="",
            wait_until="",
        )
        self._publish()

    def on_job_finished(self, task_id: str, *, outcome: str, detail: str = "") -> None:
        item = self.queue.get(task_id)
        if item is None:
            return
        status = "done" if outcome in {"done", "awaiting_audit"} else outcome
        if item.status == status:
            return
        self.queue.update(
            task_id,
            status=status,
            wait_code="" if status == "done" else status,
            wait_text=detail,
        )
        finished_at = datetime.now(UTC).replace(microsecond=0)
        elapsed = max(0.0, self.clock.monotonic() - self.started_monotonic)
        stage_seconds = self._stage_seconds(task_id)
        self.throughput.record(
            Outcome(
                task_id=task_id,
                finished_at=finished_at.isoformat(),
                outcome=status,
                queued_s=_duration_since(item.enqueued_at, finished_at),
                authoring_s=stage_seconds.get("authoring", 0.0) + stage_seconds.get("chatgpt", 0.0),
                running_s=stage_seconds.get("running", 0.0),
                delivering_s=stage_seconds.get("delivering", 0.0),
                exporting_s=stage_seconds.get("exporting", 0.0),
                total_s=elapsed,
                attempts=item.total_attempts,
                failure_kind=str((item.last_failure or {}).get("kind") or ""),
                project_id=item.project_id,
            )
        )
        self._publish()

    def _stage_seconds(self, task_id: str) -> dict[str, float]:
        result: dict[str, float] = {}
        for row in self.repo.query(
            """
            SELECT stage, started_at, ended_at FROM autopilot_stages
            WHERE task_id = ? ORDER BY started_at
            """,
            (task_id,),
        ):
            started = _parse_dt(str(row.get("started_at") or ""))
            ended = _parse_dt(str(row.get("ended_at") or ""))
            if started is None or ended is None:
                continue
            stage = str(row.get("stage") or "unknown")
            result[stage] = result.get(stage, 0.0) + max(0.0, (ended - started).total_seconds())
        return result

    def register_hold(
        self,
        *,
        source: str,
        scope: list[str],
        reason_code: str,
        reason: str,
    ) -> Hold:
        """所有挂起都由探测器自动复测、恢复后自动解除，不需要人工处理。"""
        hold = self.holds.register(
            source=source,
            scope=scope,
            reason_code=reason_code,
            reason=reason,
            probe_seconds=300,
        )
        self.repo.add_event(
            "hold.entered",
            f"新增挂起：{reason}",
            level="info",
            actor="dispatch",
            details=asdict(hold),
        )
        self.bus.publish("dispatch.hold", {"action": "entered", "hold": asdict(hold)})
        self._publish()
        return hold

    def release_hold(self, hold_id: str, *, actor: str = "user") -> Hold | None:
        return self._release_hold(hold_id, actor=actor)

    async def probe_hold(self, hold_id: str) -> ProbeResult:
        hold = self.holds.holds.get(hold_id)
        if hold is None:
            return ProbeResult(hold_id, False, "挂起不存在", {})
        result = await self.probes.probe(hold.source)
        self.repo.add_event(
            "probe.result",
            f"{hold.source} 探测{'成功' if result.ok else '失败'}：{result.message}",
            level="info" if result.ok else "warn",
            actor="probe",
            details=result.detail,
        )
        self._publish()
        return result

    async def probe_source(self, source: str) -> ProbeResult:
        result = await self.probes.probe(source)
        self.repo.add_event(
            "probe.result",
            f"{source} 探测{'成功' if result.ok else '失败'}：{result.message}",
            level="info" if result.ok else "warn",
            actor="probe",
            details=result.detail,
        )
        self.bus.publish("dispatch.log", {"seq": 0, "action": "probe.result", "message": result.message})
        return result

    def recover(self, *, since_minutes: int = 120) -> RecoveryResult:
        synthetic = Hold(
            id="manual_recovery",
            source="manual_recovery",
            scope=["containers"],
            reason_code="gateway.",
            reason=f"回看最近 {since_minutes} 分钟",
            since=(datetime.now(UTC) - timedelta(minutes=since_minutes)).replace(microsecond=0).isoformat(),
        )
        result = self.recovery.after_hold(synthetic)
        self.repo.add_event("recovery.done", f"重新排队 {result.requeued} 题", actor="user", details=result.view())
        self._publish()
        return result

    def snapshot(self) -> dict[str, Any]:
        items = self.queue.all_items()
        queue_view = [self._item_view(item) for item in self._display_order(items)]
        active = [item for item in items if item.active]
        waiting = [item for item in items if item.status in {"queued", "launching", "backoff", "interrupted"}]
        needs = [item for item in items if item.status == "quarantined"]
        finished = [item for item in items if item.status in {"done", "failed", "removed"}]
        holds = [asdict(hold) for hold in self.holds.active()]
        guards = [hit.view() for hit in self.guards.scan()]
        effective = self._effective_state(items)
        waiting_sessions = [
            item
            for item in items
            if item.session_state in {"waiting_continue", "holding", "manual_input"}
        ]
        return {
            "engine": self.config.engine,
            "enabled": self.config.enabled,
            "desired_state": self.desired_state,
            "effective_state": effective,
            "sentence": self.status_sentence(),
            "started_at": self.clock.wall().replace(microsecond=0).isoformat(),
            "uptime_seconds": round(self.clock.monotonic() - self.started_monotonic, 1),
            "settings": self.config.model_dump(),
            "limits": {
                "mode": self.config.mode,
                "containers": self.config.containers,
                "max_tasks": self.config.max_tasks,
                "create_cooldown_seconds": max(20, self.config.create_cooldown_seconds),
                "recording": self.config.recording,
                "skill_limit": self.skill_limit,
                "synced": self.limit_synced,
            },
            "pools": self.pools_book.snapshot(),
            "creation": self.creation_view(),
            "session_capacity": {
                "waiting": len(waiting_sessions),
                "running": max(0, len(active) - len(waiting_sessions)),
                "label": (
                    f"占用 {len(active)}（运行 {max(0, len(active) - len(waiting_sessions))}"
                    f" / 等待 {len(waiting_sessions)}）"
                ),
            },
            "holds": holds,
            "hold_history": self.holds.history[-20:],
            "counts": {
                "active": len(active),
                "waiting": len(waiting),
                "needs_action": len(needs),
                "finished": len(finished),
                "all": len(items),
            },
            "queue": queue_view,
            "upcoming": self.upcoming(),
            "guard": {
                "mode": self.config.guard.mode,
                "count": len(guards),
                "hits": guards,
            },
            "throughput": self.throughput.summary(),
            "capacity": self.throughput.capacity(),
            "plan": self.plan,
            "loops": self.health.snapshot(),
            "keep_awake": self.config.keep_awake,
        }

    def explain(self, task_id: str = "") -> dict[str, Any]:
        if task_id:
            item = self.queue.get(task_id)
            if item is None:
                raise KeyError(task_id)
            decision = self._decision_for(item)
            return {
                "task_id": task_id,
                "sentence": decision.text or "现在可以启动",
                "decision": decision.view(),
            }
        ordered = self.queue.ordered()
        if not ordered:
            return {"sentence": "队列为空，没有待启动任务", "decision": None}
        item = ordered[0]
        decision = self._decision_for(item, queue_head=True)
        return {"task_id": item.task_id, "sentence": decision.text or "队首可以启动", "decision": decision.view()}

    def upcoming(self, limit: int = 10) -> list[dict[str, Any]]:
        ordered = self.queue.ordered()
        # 已在执行的条目不算「接下来启动」，只列真正等待的。
        waiting = [item for item in ordered if not item.active][:limit]
        now = self.clock.wall()
        spacing = LAUNCH_SPACING_SECONDS
        items: list[dict[str, Any]] = []
        for index, item in enumerate(waiting):
            decision = self._decision_for(item, queue_head=index == 0)
            until = _parse_dt(decision.until)
            if decision.allowed:
                seconds = 0
            elif until is not None:
                seconds = max(0, int((until - now).total_seconds()))
            else:
                seconds = spacing * (index + 1)
            task = self.repo.get_task(item.task_id) or {}
            items.append(
                {
                    **item.view(queue_position=ordered.index(item) + 1),
                    "task_name": str(task.get("task_name") or item.task_id),
                    "estimated_start_at": (now + timedelta(seconds=seconds)).replace(microsecond=0).isoformat(),
                    "estimated_seconds": seconds,
                    "reason": decision.text or "可启动",
                }
            )
        return items

    def logs(
        self,
        *,
        after_seq: int = 0,
        family: str = "",
        level: str = "",
        task_id: str = "",
        limit: int = 200,
    ) -> dict[str, Any]:
        rows = self.repo.events(task_id=task_id or None, level=level or None, since_id=after_seq, limit=limit)
        items: list[dict[str, Any]] = []
        for row in rows:
            action = str(row.get("action") or "")
            if family and not action.startswith(family):
                continue
            items.append(
                {
                    **row,
                    "family": action.split(".", 1)[0],
                }
            )
        return {"items": items, "after_seq": items[-1]["id"] if items else after_seq}

    @property
    def effective_container_limit(self) -> int:
        """最大容器数是唯一的容器上限。"""
        return self.config.containers

    @property
    def tasks_first(self) -> bool:
        return self.config.mode == "tasks"

    @property
    def effective_in_hand(self) -> int:
        """在手上限。任务优先时就是最大任务数；容器优先时不限任务数，由容器占用决定能否建题。"""
        return self.config.max_tasks if self.tasks_first else MAX_IN_HAND

    @property
    def effective_session_limit(self) -> int:
        """会话上限：任务优先跟随最大任务数；容器优先时任务数可以浮动，只受会话硬上限约束。"""
        return max(1, min(MAX_SESSIONS, self.effective_in_hand))

    def in_hand_count(self) -> int:
        """已领取、仍会继续推进的任务数；需人工处理和已结束的不占在手。"""
        jobs = getattr(self.autopilot, "jobs", {}) if self.autopilot is not None else {}
        awaiting = (
            {
                task_id
                for task_id, job in jobs.items()
                if getattr(job, "desktop_running", False) and getattr(job, "awaiting_platform", False)
            }
            if isinstance(jobs, dict)
            else set()
        )
        # 已提交到 SOLO2、只等质检反馈的题不再占在手。
        return sum(
            1
            for item in self.queue.all_items(include_terminal=False)
            if item.status in IN_HAND and item.task_id not in awaiting
        )

    def status_sentence(self) -> str:
        pools = self.pools_book.snapshot()
        containers = pools["containers"]
        in_hand = self.in_hand_count()
        prefix = {
            "paused": "已暂停",
            "draining": "排空中",
            "held": "已自动挂起",
            "degraded": "运行中（部分能力受限）",
            "idle": "空闲",
            "running": "运行中",
        }[self._effective_state(self.queue.all_items())]
        demand = self.container_demand()
        parts = [
            prefix,
            "任务优先" if self.tasks_first else "容器优先",
            f"容器 {demand['used']}/{containers['limit']}",
            f"任务 {in_hand}/{self.effective_in_hand}" if self.tasks_first else f"任务 {in_hand}",
        ]
        waiting = sum(
            1
            for item in self.queue.all_items(include_terminal=False)
            if item.session_state in {"waiting_continue", "holding", "manual_input"}
        )
        if waiting:
            parts.append(f"等待继续 {waiting}")
        hold = self.holds.active()[0] if self.holds.active() else None
        if hold:
            when = hold.next_at[11:16] if len(hold.next_at) >= 16 else "稍后"
            parts.append(f"{hold.reason}，{when} 自动复测")
        elif self.desired_state == "running":
            cooldown = max(20, self.config.create_cooldown_seconds)
            elapsed = self.clock.wall().timestamp() - self.last_created_at
            countdown = max(0, int(cooldown - max(0.0, elapsed))) if self.last_created_at else 0
            if self.tasks_first and in_hand >= self.effective_in_hand:
                parts.append("任务已满，等待在手任务结束")
            elif not self.tasks_first and demand["used"] + 1 > containers["limit"]:
                parts.append("容器已满，等待容器释放")
            elif countdown:
                parts.append(f"建题冷却 {countdown} 秒")
            else:
                parts.append("可以建下一题")
        return " · ".join(parts)

    def _effective_state(self, items: list[DispatchItem]) -> str:
        if self.desired_state == "paused":
            return "paused"
        if self.desired_state == "draining":
            return "draining" if any(item.status not in {"done", "failed", "removed"} for item in items) else "paused"
        active_holds = self.holds.active()
        if any("global" in hold.scope or "containers" in hold.scope for hold in active_holds):
            return "held"
        if active_holds:
            return "degraded"
        if not items:
            return "idle"
        return "running"

    def _decision_for(self, item: DispatchItem, *, queue_head: bool = False) -> GateDecision:
        snapshot = self._fact_snapshot
        if snapshot is None:
            snapshot = FactsSnapshot(
                docker=self.facts._docker,
                disk_free_gb=self.config.disk.floor_gb,
                observed_at=0,
            )
        if item.stage in {"chatgpt", "authoring", "delivering"}:
            return evaluate_session_gates(
                item,
                settings=self.config,
                desired_state=self.desired_state,
                holds=self.holds,
                pools=self.pools_book.snapshot(),
                startup_elapsed=self.clock.monotonic() - self.started_monotonic,
                last_session_at=self.last_session_at,
                now=self.clock.wall(),
            )
        return evaluate_container_gates(
            item,
            settings=self.config,
            desired_state=self.desired_state,
            facts=snapshot,
            holds=self.holds,
            pools=self.pools_book.snapshot(),
            startup_elapsed=self.clock.monotonic() - self.started_monotonic,
            last_launch_at=self.last_launch_at,
            limit_resync_until=self.limit_resync_until,
            now=self.clock.wall(),
        )

    def _item_view(self, item: DispatchItem) -> dict[str, Any]:
        ordered = self.queue.ordered()
        position = next((index + 1 for index, candidate in enumerate(ordered) if candidate.task_id == item.task_id), None)
        decision = self._decision_for(item, queue_head=position == 1)
        task = self.repo.get_task(item.task_id) or {}
        job: Any = None
        if self.autopilot is not None:
            jobs = getattr(self.autopilot, "jobs", {})
            if isinstance(jobs, dict):
                job = jobs.get(item.task_id)
        return {
            **item.view(queue_position=position),
            "project_id": item.project_id or str(task.get("project_id") or ""),
            "engineering_id": item.engineering_id or str(task.get("engineering_id") or ""),
            "task_name": str(task.get("task_name") or item.task_id),
            "job_status": str(task.get("job_status") or ""),
            "failure_label": str((item.last_failure or {}).get("kind") or ""),
            "matched_by": str((item.last_failure or {}).get("matched_by") or ""),
            "autopilot_detail": str(getattr(job, "detail", "") or ""),
            "desktop_running": bool(getattr(job, "desktop_running", False)),
            "awaiting_platform": bool(getattr(job, "awaiting_platform", False)),
            "desktop_recovery_attempts": int(getattr(job, "desktop_recovery_attempts", 0) or 0),
            "desktop_recovery_last_at": str(getattr(job, "desktop_recovery_last_at", "") or ""),
            "desktop_recovery_error": str(getattr(job, "desktop_recovery_error", "") or ""),
            "reason": decision.text
            or ("可启动" if position == 1 else f"排队第 {position} 位" if position else item.wait_text or "已隔离，稍后自动清理并补位"),
            "gate": decision.view(),
        }

    def _display_order(self, items: list[DispatchItem]) -> list[DispatchItem]:
        return sorted(
            items,
            key=lambda item: (
                item.status not in {"quarantined"} and item.status not in {"active", "launching"},
                item.status in {"done", "failed", "removed"},
                -item.priority,
                item.position,
            ),
        )

    def _configure_pools(self) -> None:
        agents = self.effective_session_limit
        self.pools_book.configure(
            containers=self.effective_container_limit,
            sessions=agents,
            authoring=agents,
            delivering=agents,
            recording=self.config.recording,
        )

    def _sync_runtime_pools(self) -> None:
        if self.autopilot is None:
            return
        jobs = getattr(self.autopilot, "jobs", {})
        agents = getattr(self.autopilot, "agents", {})
        sessions: set[str] = set()
        authoring: set[str] = set()
        delivering: set[str] = set()
        if isinstance(jobs, dict):
            for task_id, job in jobs.items():
                key = f"task:{task_id}"
                desktop_running = bool(getattr(job, "desktop_running", False))
                agent_running = task_id in agents
                # 已提交、只在等 SOLO2 质检结果的会话不占会话名额。
                if desktop_running and not bool(getattr(job, "awaiting_platform", False)):
                    sessions.add(key)
                if agent_running:
                    stage = str(getattr(agents[task_id], "stage", ""))
                    if stage == "authoring":
                        authoring.add(key)
                    elif stage == "delivering":
                        delivering.add(key)
                item = self.queue.get(task_id)
                stage_name = str(getattr(job, "stage", "pending"))
                if item is not None and stage_name not in {"awaiting_audit", "done", "failed", "stopped"}:
                    desired_status = "active" if desktop_running or agent_running else "queued"
                    if item.status != desired_status or item.stage != stage_name:
                        self.queue.update(task_id, status=desired_status, stage=stage_name)
                elif item is not None and item.status in {"queued", "launching", "active"}:
                    # 作业已结束（待抽检/失败/停止）但队列项仍挂着：补记收尾，避免虚占在手。
                    self.on_job_finished(task_id, outcome=stage_name, detail=str(getattr(job, "detail", "")))
        orphan_keys = getattr(self.autopilot, "orphan_session_keys", None)
        if isinstance(orphan_keys, set):
            # 平台已放弃跟踪、但在 ChatGPT 里仍在运行的会话照样消耗用量，必须占名额。
            sessions |= orphan_keys
        self.pools_book.observe("sessions", sessions)
        self.pools_book.observe("authoring", authoring)
        self.pools_book.observe("delivering", delivering)

    async def _facts_loop(self, name: str, interval: float) -> None:
        while not self._stopping:
            async with self.health.beat(name):
                self._fact_snapshot = await self.facts.snapshot(force=True)
                self.pools_book.occupy_from_facts(self._fact_snapshot)
                self._sync_runtime_pools()
                self._configure_pools()
                self._check_clock()
            await asyncio.sleep(interval)

    async def _gate_loop(self, name: str, interval: float) -> None:
        while not self._stopping:
            async with self.health.beat(name):
                await self._refresh_waits()
                current = self.clock.monotonic()
                if current - self._last_limit_sync >= 60:
                    self._last_limit_sync = current
                    await self.sync_skill_limit(sync=True)
            await asyncio.sleep(interval)

    async def _probe_loop(self, name: str, interval: float) -> None:
        while not self._stopping:
            async with self.health.beat(name):
                current = self.clock.monotonic()
                if self.config.llm_guard.enabled and current >= self._probe_next_at and "llm_guard" not in self.holds.holds:
                    result = await self.probes.probe("llm_guard")
                    if result.ok:
                        delay = self.config.llm_guard.probe_seconds
                    elif "llm_guard" in self.holds.holds:
                        delay = self.config.llm_guard.paused_probe_seconds
                    else:
                        delay = self.config.llm_guard.retry_seconds
                    self._probe_next_at = current + delay
                if current >= self._probe_next_docker:
                    docker_result = await self.probes.probe("docker_guard")
                    self._probe_next_docker = current + (10 if not docker_result.ok else 30)
                await self.probes.run_due()
                snapshot = self._fact_snapshot or await self.facts.snapshot()
                if snapshot.disk_free_gb < self.config.disk.floor_gb:
                    await self.probes.probe("disk_guard")
                else:
                    self.holds.release("disk_guard", actor="probe")
                self._sample_capacity()
            await asyncio.sleep(interval)

    async def _retry_loop(self, name: str, interval: float) -> None:
        while not self._stopping:
            async with self.health.beat(name):
                await self._retry_due()
            await asyncio.sleep(interval)

    async def _retry_due(self) -> None:
        if self.desired_state != "running":
            return
        for item in self.queue.due_retry():
            if self.autopilot is None:
                break
            try:
                await self.autopilot.retry(item.task_id)
            except Exception:
                LOGGER.exception("automatic retry failed: %s", item.task_id)
            else:
                self.queue.update(item.task_id, status="queued", not_before="", wait_code="", wait_text="", wait_until="")

    async def _refresh_waits(self) -> None:
        ordered = self.queue.ordered()
        for index, item in enumerate(ordered):
            decision = self._decision_for(item, queue_head=index == 0)
            text = decision.text or (f"排队第 {index + 1} 位" if index else "可启动")
            code = decision.code or ("queue.waiting" if index else "gate.ready")
            if item.wait_code != code or item.wait_text != text or item.wait_until != decision.until:
                self.queue.update(item.task_id, wait_code=code, wait_text=text, wait_until=decision.until)
                if index == 0 or decision.code not in {"capacity.full", "spacing", "startup.grace"}:
                    self._event_once(
                        f"wait:{item.task_id}:{code}",
                        "gate.waiting" if not decision.allowed else "gate.ready",
                        text,
                        task_id=item.task_id,
                        details={"code": code, "gate": decision.gate},
                    )
        self._publish()

    def _release_hold(self, source: str, *, actor: str) -> Hold | None:
        hold = self.holds.release(source, actor=actor)
        if hold is None:
            return None
        recovery = self.recovery.after_hold(hold)
        self.repo.add_event(
            "hold.released",
            f"挂起已解除：{hold.reason}",
            actor=actor,
            details={"hold": asdict(hold), "recovery": recovery.view()},
        )
        self.bus.publish("dispatch.hold", {"action": "released", "hold": asdict(hold)})
        self.bus.publish("dispatch.log", {"seq": 0, "action": "hold.released", "message": hold.reason})
        if recovery.task_ids:
            with suppress(RuntimeError):
                asyncio.create_task(self._retry_recovered(recovery.task_ids))
        self._publish()
        return hold

    async def _retry_recovered(self, task_ids: list[str]) -> None:
        if self.autopilot is None:
            return
        for task_id in task_ids:
            with suppress(Exception):
                await self.autopilot.retry(task_id)

    def _event_once(
        self,
        key: str,
        action: str,
        message: str,
        *,
        level: str = "info",
        task_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        signature = f"{action}:{message}"
        if self._event_signatures.get(key) == signature:
            return
        self._event_signatures[key] = signature
        self.repo.add_event(action, message, level=level, actor="dispatch", task_id=task_id, details=details)
        self.bus.publish(
            "dispatch.log",
            {"seq": self.bus.sequence, "action": action, "message": message, "task_id": task_id},
        )

    def _sample_capacity(self) -> None:
        now = self.clock.monotonic()
        if now - self._last_capacity_sample < 60:
            return
        self._last_capacity_sample = now
        pool = self.pools_book.snapshot()
        self.throughput.sample_capacity(
            occupied=int(pool["containers"]["used"]),
            limit_effective=int(pool["containers"]["limit"]),
            limit_floor=self.config.containers,
            in_hand=self.in_hand_count(),
            sessions=int(pool["sessions"]["used"]),
            held=[hold.source for hold in self.holds.active()],
            waiting=sum(
                1
                for item in self.queue.all_items(include_terminal=False)
                if item.session_state in {"waiting_continue", "holding", "manual_input"}
            ),
        )

    def _check_clock(self) -> None:
        self._clock_state = observe_clock(self._clock_state, self.clock)
        if self._clock_state.jumped_back:
            self.repo.add_event("system.clock_jump", "检测到系统时间回拨，计时继续使用单调时钟", level="warn", actor="dispatch")
        if self._clock_state.slept_seconds:
            # Keep the marker so diagnostics show that sleep was accounted for.
            self._event_once(
                "system.wake",
                "system.wake",
                f"检测到睡眠 {int(self._clock_state.slept_seconds)} 秒，已从超时计时中扣除",
                details={"slept_seconds": self._clock_state.slept_seconds},
            )

    async def _session_probe(self) -> dict[str, Any]:
        if self.autopilot is None:
            return {"ok": False, "message": "执行器尚未装配"}
        agent_mode = getattr(getattr(self.autopilot, "settings", None), "agent_mode", "chatgpt")
        if agent_mode == "chatgpt":
            limited = getattr(self.autopilot, "usage_limited", None)
            if callable(limited) and limited():
                return {"ok": False, "message": "ChatGPT 仍处于用量限制冷却期"}
            desktop = getattr(self.autopilot, "desktop", None)
            available = bool(desktop and desktop.available())
            return {"ok": available, "message": "ChatGPT 桌面端可用" if available else "ChatGPT 桌面端不可用"}
        command = getattr(self.autopilot, "agent_command", [])
        binary = str(command[0]) if command else ""
        available = bool(binary and shutil.which(binary))
        return {"ok": available, "message": f"Agent 可执行：{binary}" if available else f"找不到 Agent：{binary}"}

    async def _skill_probe(self) -> dict[str, Any]:
        try:
            version = await self.gateway.version()
        except Exception as exc:
            return {"ok": False, "message": str(exc)}
        return {"ok": bool(version), "message": f"技能可用，版本 {version}"}

    async def _recording_probe(self) -> dict[str, Any]:
        service = self.recording
        if service is None:
            return {"ok": True, "message": "录屏服务未装配"}
        if hasattr(service, "status"):
            try:
                value = service.status()
                if asyncio.iscoroutine(value):
                    value = await value
                return {"ok": True, "message": "录屏服务可用", "detail": value}
            except Exception as exc:
                return {"ok": False, "message": str(exc)}
        return {"ok": True, "message": "录屏服务可用"}

    def _close_legacy_quarantine(self) -> None:
        """旧版本把失败题放进“待处理”等人决定；现在统一按失败结束，交给自动清理补新题。"""
        for item in self.queue.all_items(include_terminal=False):
            if item.status == "quarantined":
                self.queue.update(
                    item.task_id,
                    status="failed",
                    wait_code="gave_up",
                    wait_text=item.wait_text or "失败已记录",
                    wait_until="",
                )

    def _migrate_queue(self) -> None:
        if self.queue.all_items():
            return
        autopilot_state = read_json(self.paths.home / "autopilot.json", {})
        intake_state = read_json(self.paths.home / "intake.json", {})
        count = self.queue.migrate(
            autopilot_state if isinstance(autopilot_state, dict) else {},
            intake_state if isinstance(intake_state, dict) else {},
        )
        if count:
            self.repo.add_event(
                "dispatch.migrated",
                f"已从 v2 状态迁移 {count} 个未终态任务",
                actor="dispatch",
                details={"dispatch_items": count},
            )

    def _load_state(self) -> dict[str, Any]:
        data = read_json(self.state_file, {})
        return data if isinstance(data, dict) else {}

    def _save_state(self) -> None:
        self.state = {
            "desired_state": self.desired_state,
            "plan": self.plan,
            "last_created_at": self.last_created_at,
            "updated_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        }
        write_json(self.state_file, self.state)

    def _record_start(self) -> None:
        now = self.clock.wall()
        self._start_id = now.replace(microsecond=0).isoformat()
        starts = self._starts()
        starts.append(
            {
                "start_id": self._start_id,
                "started_at": self._start_id,
                "clean_exit": False,
                "pid": os.getpid(),
                "version": "0.4.0",
            }
        )
        write_json(self.starts_file, starts[-20:])

    def _record_clean_exit(self) -> None:
        starts = self._starts()
        if not starts:
            return
        for item in reversed(starts):
            if item.get("start_id") == self._start_id:
                item["clean_exit"] = True
                item["finished_at"] = self.clock.wall().replace(microsecond=0).isoformat()
                break
        write_json(self.starts_file, starts[-20:])

    def _starts(self) -> list[dict[str, Any]]:
        value = read_json(self.starts_file, [])
        return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []

    def _recent_unclean_starts(self) -> int:
        cutoff = self.clock.wall() - timedelta(minutes=15)
        count = 0
        for item in self._starts():
            if item.get("clean_exit") or item.get("start_id") == self._start_id:
                continue
            try:
                started = datetime.fromisoformat(str(item.get("started_at") or ""))
            except ValueError:
                continue
            if started.tzinfo is None:
                started = started.astimezone()
            if started >= cutoff:
                count += 1
        return count

    def _publish(self) -> None:
        self.bus.publish("dispatch.updated", {"seq": self.bus.sequence})
        self.bus.publish("autopilot.updated", {"active": sum(1 for item in self.queue.all_items() if item.active)})


NEEDS_CONTAINER = frozenset({"", "created", "prepared", "prompt_ready", "running"})


def _task_run_state(task_root: Path) -> tuple[str, str, float]:
    """(state.status, 最近一次 attempt 的容器名, state.json mtime)；读不到时按“还需要容器”。"""
    path = task_root / "monitor" / "state.json"
    try:
        stat = path.stat()
        data = read_json(path, {})
    except OSError:
        return "", "", 0.0
    if not isinstance(data, dict):
        return "", "", stat.st_mtime
    attempts = data.get("attempts") if isinstance(data.get("attempts"), list) else []
    container = ""
    if attempts and isinstance(attempts[-1], dict):
        container = str(attempts[-1].get("container") or "")
    session = data.get("sessionRun")
    if isinstance(session, dict) and session.get("container"):
        container = str(session["container"])
    return str(data.get("status") or ""), container, stat.st_mtime


def _parse_dt(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.astimezone()


def _duration_since(value: str, finished: datetime) -> float:
    started = _parse_dt(value)
    return round(max(0.0, (finished - started).total_seconds()), 1) if started else 0.0


def _diff(before: dict[str, Any], after: dict[str, Any], prefix: str = "") -> dict[str, list[Any]]:
    changed: dict[str, list[Any]] = {}
    for key in sorted(set(before) | set(after)):
        old, new = before.get(key), after.get(key)
        name = f"{prefix}{key}"
        if isinstance(old, dict) and isinstance(new, dict):
            changed.update(_diff(old, new, f"{name}."))
        elif old != new:
            changed[name] = [old, new]
    return changed
