"""Container-aware task queue and asynchronous run lifecycle."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from solo_auto.dispatch.classify import Evidence
from solo_auto.dispatch.facts import docker_info_ok, docker_running_containers
from solo_auto.errors import ApiError
from solo_auto.events.bus import EventBus
from solo_auto.gateway.skill import SkillGateway, StreamEvent
from solo_auto.pipeline.reconcile import Reconciler
from solo_auto.settings import Paths
from solo_auto.store.repo import Repository
from solo_auto.store.state import load_state

if TYPE_CHECKING:
    from solo_auto.dispatch.kernel import Dispatcher

LOGGER = logging.getLogger(__name__)
CLAUDE_LAUNCH_CALL_TIMEOUT_SECONDS = 300
MANAGED_ACTIVE_STATES = {
    "launching",
    "prompt_delivery",
    "prompt_sent",
    "running",
    "continuing",
    "holding",
    "manual_input",
    "closing",
}


@dataclass(slots=True)
class QueueEntry:
    task_id: str
    task_name: str
    task_root: Path
    enqueued_at: float = field(default_factory=time.monotonic)
    pinned: bool = False


@dataclass(slots=True)
class ActiveRun:
    entry: QueueEntry
    started_at: float
    phase: str = "queued"
    attempt: int = 0
    log_lines: list[str] = field(default_factory=list)
    task: asyncio.Task[None] | None = None


class RunScheduler:
    def __init__(
        self,
        *,
        paths: Paths,
        gateway: SkillGateway,
        repo: Repository,
        reconciler: Reconciler,
        bus: EventBus,
        dispatcher: Dispatcher | None = None,
        attempt_total: int = 3,
        run_mode: str = "supervised",
    ) -> None:
        self.paths = paths
        self.gateway = gateway
        self.repo = repo
        self.reconciler = reconciler
        self.bus = bus
        self.dispatcher = dispatcher
        self.attempt_total = max(1, attempt_total)
        self.run_mode = run_mode
        self.queue: list[QueueEntry] = []
        self.active: dict[str, ActiveRun] = {}
        self.limit = 4
        self._tick_task: asyncio.Task[None] | None = None
        self._maintenance_task: asyncio.Task[None] | None = None
        self._stopping = False
        self._last_log_publish: dict[str, float] = {}
        self._retry_tasks: set[asyncio.Task[None]] = set()
        self._waiting_since: dict[str, float] = {}
        self.intake: Any = None
        self.ledger: Any = None

    def attach_recovery(self, *, intake: Any = None, ledger: Any = None) -> None:
        self.intake = intake
        self.ledger = ledger

    async def start(self) -> None:
        if self.dispatcher is not None and self.dispatcher.v3_enabled:
            self.limit = self.dispatcher.effective_container_limit
        else:
            await self.refresh_limit()
        await self._adopt_legacy_print_runs()
        self._tick_task = asyncio.create_task(self._tick_loop())
        self._maintenance_task = asyncio.create_task(self._maintenance_loop())

    async def stop(self) -> None:
        self._stopping = True
        for task in (self._tick_task, self._maintenance_task):
            if task:
                task.cancel()
        for task in self._retry_tasks:
            task.cancel()
        await asyncio.gather(
            *(
                task
                for task in (self._tick_task, self._maintenance_task, *self._retry_tasks)
                if task
            ),
            return_exceptions=True,
        )

    async def refresh_limit(self) -> int:
        result = await self.gateway.call(["config", "show"], timeout=30)
        if result.code == 0 and result.data:
            claude = result.data.get("claude")
            if isinstance(claude, dict):
                try:
                    self.limit = max(1, min(8, int(claude.get("maxContainers") or 4)))
                except (TypeError, ValueError):
                    self.limit = 4
        return self.limit

    async def enqueue(self, task_id: str, *, pinned: bool = False) -> dict[str, Any]:
        if self._stopping:
            raise ApiError(503, "SCHEDULER_STOPPING", "调度器正在停止")
        if task_id in self.active:
            raise ApiError(409, "ALREADY_RUNNING", "该任务已在运行中")
        if any(entry.task_id == task_id for entry in self.queue):
            raise ApiError(409, "ALREADY_QUEUED", "该任务已在队列中")
        task = self.repo.get_task(task_id)
        if not task:
            raise ApiError(404, "TASK_NOT_FOUND", f"任务不存在：{task_id}")
        root = Path(task["task_root"])
        envelope = load_state(root)
        if not envelope.compatible or envelope.state is None:
            raise ApiError(409, "TASK_STATE_INCOMPATIBLE", envelope.error or "任务状态不可读")
        state = envelope.state
        if state.status not in {"prompt_ready", "run_failed"}:
            raise ApiError(409, "TASK_NOT_READY", f"当前状态为 {state.status}，不能运行")
        if not state.remote.initialPushed:
            raise ApiError(409, "INITIAL_SNAPSHOT_NOT_PUSHED", "初始快照尚未推送")
        if state.session:
            raise ApiError(409, "SESSION_ALREADY_ACCEPTED", "该题已有被接受的会话")
        entry = QueueEntry(task_id, str(task["task_name"]), root, pinned=pinned)
        self.queue.append(entry)
        self.queue.sort(key=lambda item: (not item.pinned, item.enqueued_at))
        self.repo.add_event("run.queued", f"{entry.task_name} 已入队", actor="user", task_id=task_id)
        self.bus.publish("queue.updated", self.queue_snapshot())
        state_value = "running" if task_id in self.active else "queued"
        return {"state": state_value, "queue_position": self.queue_position(task_id)}

    async def stop_task(self, task_id: str, *, force: bool = False) -> dict[str, Any]:
        for index, entry in enumerate(list(self.queue)):
            if entry.task_id == task_id:
                self.queue.pop(index)
                self.repo.add_event("run.dequeued", f"{entry.task_name} 已移出队列", actor="user", task_id=task_id)
                self.bus.publish("queue.updated", self.queue_snapshot())
                return {"state": "dequeued", "task_id": task_id}
        active = self.active.get(task_id)
        if not active:
            raise ApiError(404, "RUN_NOT_FOUND", "该任务没有排队或运行中的实例")
        aborted = await self.gateway.call(
            [
                "abort",
                "--task-root",
                str(active.entry.task_root),
                "--reason",
                "用户停止" if force else "用户请求停止",
                "--by",
                "human",
            ],
            timeout=90,
            task_id=task_id,
        )
        if aborted.code != 0:
            raise ApiError(
                500,
                "SESSION_ABORT_FAILED",
                aborted.err.strip() or "技能终止会话失败",
                detail={"stdout": aborted.out, "stderr": aborted.err},
            )
        return {"state": "stopping", "task_id": task_id}

    def queue_position(self, task_id: str) -> int | None:
        for index, entry in enumerate(self.queue, 1):
            if entry.task_id == task_id:
                return index
        return None

    def queue_snapshot(self) -> dict[str, Any]:
        return {
            "limit": self.limit,
            "used": len(self.active),
            "slots": [
                {
                    "task_id": run.entry.task_id,
                    "task_name": run.entry.task_name,
                    "phase": run.phase,
                    "attempt": run.attempt,
                    "started_at": datetime.fromtimestamp(run.started_at).astimezone().isoformat(),
                }
                for run in self.active.values()
            ],
            "waiting": [
                {
                    "position": index,
                    "task_id": entry.task_id,
                    "task_name": entry.task_name,
                    "pinned": entry.pinned,
                    "enqueued_at": datetime.fromtimestamp(entry.enqueued_at).astimezone().isoformat(),
                }
                for index, entry in enumerate(self.queue, 1)
            ],
        }

    def run_status(self, task_id: str, task_root: Path | None = None) -> dict[str, Any]:
        active = self.active.get(task_id)
        if active:
            shadow = _read_json(self.paths.sessions / f"{task_id}.json")
            return {
                "state": "running",
                "attempt_seq": active.attempt,
                "attempt_total": self.attempt_total,
                "phase": active.phase,
                "round": int(shadow.get("round") or 0),
                "continues": shadow.get("continues") or {"used": 0, "max": 0},
                "next_action_at": shadow.get("nextActionAt") or "",
                "session_state": shadow.get("sessionState") or "",
                "last_kind": shadow.get("lastKind") or "",
                "started_at": datetime.fromtimestamp(active.started_at).astimezone().isoformat(),
                "elapsed_s": round(time.monotonic() - active.started_at, 1),
                "log_tail": "\n".join(active.log_lines[-200:]),
            }
        status = self.queue_position(task_id)
        if status:
            return {
                "state": "queued",
                "attempt_seq": 0,
                "attempt_total": self.attempt_total,
                "phase": "queued",
                "round": 0,
                "continues": {"used": 0, "max": 0},
                "next_action_at": "",
                "session_state": "queued",
                "last_kind": "",
                "started_at": "",
                "elapsed_s": 0,
                "queue_position": status,
                "log_tail": "",
            }
        attempt_seq = 0
        attempt_total = self.attempt_total
        phase = "idle"
        started_at = ""
        elapsed = 0.0
        log_tail = ""
        shadow = _read_json(self.paths.sessions / f"{task_id}.json")
        round_count = int(shadow.get("round") or 0)
        continues_value: dict[str, Any] = shadow.get("continues") or {"used": 0, "max": 0}
        next_action_at = shadow.get("nextActionAt") or ""
        session_state = shadow.get("sessionState") or ""
        last_kind = shadow.get("lastKind") or ""
        if task_root is not None:
            envelope = load_state(task_root)
            if envelope.state is not None:
                state = envelope.state
                attempt_seq = len(state.attempts)
                run_state = state.sessionRun
                if run_state is not None:
                    round_count = len(run_state.rounds)
                    continues_value = {
                        "used": run_state.continues.used,
                        "max": run_state.continues.max,
                        "history": [
                            item.model_dump(by_alias=True)
                            for item in run_state.continues.history
                        ],
                    }
                    next_action_at = run_state.nextActionAt or ""
                    session_state = {
                        "running": "running",
                        "continuing": "waiting_continue",
                        "holding": "holding",
                        "manual_input": "manual_input",
                        "accepted": "accepted",
                        "aborted": "aborted",
                        "session_lost": "session_lost",
                    }.get(run_state.status, run_state.status)
                    last_kind = (
                        run_state.rounds[-1].kind
                        if run_state.rounds
                        else ""
                    )
                if state.attempts:
                    last = state.attempts[-1]
                    started_at = last.startedAt
                    elapsed = _elapsed(last.startedAt, last.finishedAt)
                    phase = {
                        "accepted": "done",
                        "running": "running",
                        "pending_collect": "running",
                        "rejected": "failed",
                        "cancelled": "failed",
                    }.get(last.status, state.status)
                    directory = Path(last.dir) if last.dir else None
                    if directory:
                        parts: list[str] = []
                        for name in ("stderr.log", "stdout.jsonl"):
                            path = directory / name
                            if path.is_file():
                                lines = _tail_lines(path, 120)
                                parts.extend(f"{name}: {line}" for line in lines)
                        log_tail = "\n".join(parts[-200:])
        return {
            "state": "idle",
            "attempt_seq": attempt_seq,
            "attempt_total": attempt_total,
            "phase": phase,
            "round": round_count,
            "continues": continues_value,
            "next_action_at": next_action_at,
            "session_state": session_state,
            "last_kind": last_kind,
            "started_at": started_at,
            "elapsed_s": elapsed,
            "log_tail": log_tail,
        }

    async def _tick_loop(self) -> None:
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("scheduler tick failed")
            await asyncio.sleep(2)

    async def _tick(self) -> None:
        if self.dispatcher is not None and self.dispatcher.v3_enabled:
            if not self.queue:
                return
            decision = await self.dispatcher.request_container(self.queue[0].task_id)
            if not decision.allowed:
                return
        else:
            await self.refresh_limit()
            containers = await _running_containers()
            self._clean_slot_files(containers)
            used = max(len(containers), len(self.active))
            if used >= self.limit or not self.queue:
                return
            if not containers and not self.active and not await _docker_ok():
                return
        entry = self.queue.pop(0)
        run = ActiveRun(entry=entry, started_at=time.time())
        self.active[entry.task_id] = run
        self._write_slot(entry, "starting")
        run.task = asyncio.create_task(self._execute(run))
        if self.dispatcher is not None and self.dispatcher.v3_enabled:
            self.dispatcher.mark_container_started(entry.task_id)
        self.repo.add_event("run.started", f"{entry.task_name} 开始运行", actor="scheduler", task_id=entry.task_id)
        self.bus.publish("queue.updated", self.queue_snapshot())

    async def _execute(self, run: ActiveRun) -> None:
        task_id = run.entry.task_id
        if self.run_mode == "print":
            await self._execute_print(run)
            return
        seen_events: set[str] = set()
        poll_failures = 0
        try:
            started = await self.gateway.call(
                ["run", "--task-root", str(run.entry.task_root)],
                timeout=CLAUDE_LAUNCH_CALL_TIMEOUT_SECONDS,
                task_id=task_id,
            )
            if started.code != 0 or not started.data:
                recovered = await self._recover_launch_result(task_id, run.entry.task_root)
                if recovered is None:
                    raise RuntimeError(
                        started.err.strip()
                        or started.out.strip()
                        or f"技能启动失败，exit={started.code}"
                    )
                started_data = recovered
                self.repo.add_event(
                    "run.launch_recovered",
                    f"{run.entry.task_name} 的启动调用返回异常，但受管会话已成功接管",
                    level="warn",
                    actor="scheduler",
                    task_id=task_id,
                    details={"status": recovered},
                )
            else:
                started_data = started.data
            run.attempt = int(started_data.get("attempt") or started_data.get("attemptIndex") or 0)
            run.phase = "running"
            self.repo.add_event(
                "run.started.managed",
                f"{run.entry.task_name} 已发起受管会话",
                actor="scheduler",
                task_id=task_id,
                details=started_data,
            )
            while True:
                await asyncio.sleep(2)
                result = await self.gateway.run_status(run.entry.task_root, task_id=task_id)
                if result.code != 0 or result.data is None:
                    poll_failures += 1
                    if poll_failures == 3:
                        resumed = await self.gateway.call(
                            ["resume", "--task-root", str(run.entry.task_root)],
                            timeout=60,
                            task_id=task_id,
                        )
                        if resumed.code == 0:
                            poll_failures = 0
                            self.repo.add_event(
                                "session.resumed",
                                f"{run.entry.task_name} 的监督器已接回",
                                actor="scheduler",
                                task_id=task_id,
                                details=resumed.data or {},
                            )
                            self.bus.publish(
                                "session.resumed",
                                {"task_id": task_id, "detail": resumed.data or {}},
                            )
                            continue
                        self.repo.set_job_status(
                            task_id, "blocked", notes="监督器丢失且无法接回"
                        )
                        run.phase = "blocked"
                        return
                    continue
                poll_failures = 0
                status = result.data
                self._write_session_shadow(task_id, status)
                self._sync_session_state(task_id, run, status, seen_events)
                session_state = str(status.get("sessionState") or "")
                if session_state == "waiting_continue":
                    run.phase = "waiting"
                    await self._account_waiting_continue(task_id, status)
                elif session_state in {"holding", "manual_input"}:
                    self._waiting_since.pop(task_id, None)
                    run.phase = "blocked"
                    if session_state == "holding" and str(status.get("lastKind") or "") == "auth":
                        await self._trip_auth_breaker(task_id, "会话等待鉴权处理")
                elif status.get("status") == "accepted":
                    run.phase = "done"
                    self.repo.add_event(
                        "run.completed",
                        f"{run.entry.task_name} 运行完成",
                        actor="scheduler",
                        task_id=task_id,
                        details=status,
                    )
                    self.bus.publish(
                        "toast",
                        {
                            "level": "info",
                            "message": f"{run.entry.task_name} 运行结束",
                            "task_id": task_id,
                        },
                    )
                    return
                elif status.get("status") == "start_failed":
                    run.phase = "failed"
                    detail = str(
                        status.get("error")
                        or "Claude Code 在会话成立前启动失败"
                    )
                    await self._record_run_failure(
                        task_id,
                        kind="run.start_failed",
                        detail=detail,
                        status=status,
                    )
                    return
                elif status.get("status") in {"aborted", "session_lost"}:
                    run.phase = "failed"
                    reason = str(
                        (status.get("abort") or {}).get("reason")
                        or (
                            "session_lost"
                            if status.get("status") == "session_lost"
                            else "session_aborted"
                        )
                    )
                    detail = str(
                        (status.get("abort") or {}).get("detail")
                        or status.get("status")
                        or reason
                    )
                    await self._record_run_failure(
                        task_id,
                        kind=_run_failure_kind(reason),
                        detail=detail,
                        status=status,
                    )
                    return
                else:
                    self._waiting_since.pop(task_id, None)
                    run.phase = "running"
        except asyncio.CancelledError:
            self.repo.add_event(
                "run.detached",
                "工作台停止跟踪；技能监督器与容器继续运行",
                level="warn",
                actor="scheduler",
                task_id=task_id,
            )
            raise
        except Exception as exc:
            run.phase = "failed"
            LOGGER.exception("run task %s failed", task_id)
            await self._record_run_failure(
                task_id,
                kind="run.start_failed",
                detail=str(exc),
                status={},
            )
            self.repo.add_event(
                "run.failed",
                str(exc),
                level="error",
                actor="gateway",
                task_id=task_id,
            )
        finally:
            self.active.pop(task_id, None)
            self._waiting_since.pop(task_id, None)
            (self.paths.slots / f"{task_id}.json").unlink(missing_ok=True)
            if self.dispatcher is not None and self.dispatcher.v3_enabled:
                if run.phase == "done":
                    self.dispatcher.on_success(task_id, stage="running")
                self.dispatcher.release_container(task_id, success=run.phase == "done")
            with suppress(Exception):
                await self.gateway.call(["terminal-sweep"], timeout=30, task_id=task_id)
            try:
                await self.reconciler.run()
            except Exception:
                LOGGER.exception("post-run reconcile failed")
            self.bus.publish("queue.updated", self.queue_snapshot())
            self.bus.publish("task.updated", {"task_id": task_id, "phase": run.phase})

    async def _recover_launch_result(self, task_id: str, task_root: Path) -> dict[str, Any] | None:
        """Adopt a managed session when the launcher timed out after tmux started."""
        for attempt in range(3):
            status_result = await self.gateway.run_status(task_root, task_id=task_id)
            status = status_result.data if status_result.code == 0 else None
            if isinstance(status, dict):
                state = str(status.get("status") or "")
                alive = bool(
                    status.get("supervisorAlive")
                    or status.get("containerRunning")
                    or status.get("tmuxAlive")
                )
                if state in MANAGED_ACTIVE_STATES and alive:
                    if not status.get("supervisorAlive"):
                        resumed = await self.gateway.call(
                            ["resume", "--task-root", str(task_root)],
                            timeout=60,
                            task_id=task_id,
                        )
                        if resumed.code != 0:
                            return None
                    return status
                if state == "accepted":
                    return status
            if attempt < 2:
                await asyncio.sleep(2)
        return None

    async def _account_waiting_continue(
        self,
        task_id: str,
        status: dict[str, Any],
    ) -> None:
        if self.dispatcher is None or not self.dispatcher.v3_enabled:
            return
        started = self._waiting_since.setdefault(task_id, time.monotonic())
        stalled = time.monotonic() - started
        if stalled < 600:
            return
        pool = self.dispatcher.pools_book.snapshot()["containers"]
        action = await self.dispatcher.elastic.adjust(
            stall_seconds=stalled,
            full=int(pool["used"]) >= int(pool["limit"]),
            waiting=True,
            severe=str(status.get("lastKind") or "") == "rate_limited",
        )
        if action:
            self.dispatcher._configure_pools()
            self.repo.add_event(
                f"elastic.{action}",
                f"等待继续持续 {int(stalled)} 秒，弹性容器上限调整为 "
                f"{self.dispatcher.effective_container_limit}",
                actor="scheduler",
                task_id=task_id,
                details={"kind": status.get("lastKind"), "stall": int(stalled)},
            )

    async def _execute_print(self, run: ActiveRun) -> None:
        """Long-lived compatibility path for the explicit print rollback."""
        task_id = run.entry.task_id

        async def on_event(event: StreamEvent) -> None:
            run.log_lines.append(event.line)
            run.log_lines = run.log_lines[-500:]
            phase = _phase_from_line(event.line)
            if phase:
                run.phase = phase
            if event.kind == "stderr":
                self.bus.publish(
                    "run.log",
                    {"task_id": task_id, "line": event.line, "kind": event.kind, "batch": True},
                )

        try:
            streamed = await self.gateway.stream(
                ["run", "--task-root", str(run.entry.task_root), "--mode", "print"],
                task_root=run.entry.task_root,
                on_event=on_event,
                timeout=None,
                task_id=task_id,
                cancel_key=task_id,
            )
            result = streamed.result
            if result.code == 0 and result.data and result.data.get("ok"):
                run.phase = "done"
                self.repo.add_event(
                    "run.completed",
                    f"{run.entry.task_name} 运行完成",
                    actor="scheduler",
                    task_id=task_id,
                    details=result.data,
                )
            else:
                run.phase = "failed"
                detail = result.err.strip() or result.out.strip() or f"exit={result.code}"
                await self._record_run_failure(
                    task_id,
                    kind="run.incomplete",
                    detail=detail,
                    status={},
                )
        except asyncio.CancelledError:
            run.phase = "failed"
            raise
        except Exception as exc:
            run.phase = "failed"
            await self._record_run_failure(
                task_id,
                kind="run.start_failed",
                detail=str(exc),
                status={},
            )
        finally:
            self.active.pop(task_id, None)
            (self.paths.slots / f"{task_id}.json").unlink(missing_ok=True)
            if self.dispatcher is not None and self.dispatcher.v3_enabled:
                if run.phase == "done":
                    self.dispatcher.on_success(task_id, stage="running")
                self.dispatcher.release_container(task_id, success=run.phase == "done")
            try:
                await self.reconciler.run()
            except Exception:
                LOGGER.exception("post-run reconcile failed")
            self.bus.publish("queue.updated", self.queue_snapshot())
            self.bus.publish("task.updated", {"task_id": task_id, "phase": run.phase})

    async def _adopt_legacy_print_runs(self) -> None:
        """Adopt pre-v4 print containers that survived a workbench restart."""
        live = await _running_containers()
        for task in self.repo.query(
            "SELECT id, task_root, task_name, job_status FROM tasks "
            "WHERE job_status IN ('running','blocked')"
        ):
            task_id = str(task["id"])
            if task_id in self.active:
                continue
            root = Path(str(task["task_root"]))
            envelope = load_state(root)
            if envelope.state is None or envelope.state.sessionRun is not None:
                continue
            attempt = next(
                (
                    item
                    for item in reversed(envelope.state.attempts)
                    if item.container
                    and item.status in {"running", "pending_collect"}
                ),
                None,
            )
            if attempt is None or attempt.container not in live:
                continue
            entry = QueueEntry(
                task_id=task_id,
                task_name=str(task["task_name"]),
                task_root=root,
            )
            run = ActiveRun(
                entry=entry,
                started_at=time.time(),
                phase="running",
                attempt=attempt.index,
            )
            self.active[task_id] = run
            run.task = asyncio.create_task(self._resume_legacy_print(run))
            self.repo.add_event(
                "run.legacy_adopted",
                f"{entry.task_name} 的旧版 print 容器已由重启后的工作台认领",
                actor="scheduler",
                task_id=task_id,
                details={"container": attempt.container, "attempt": attempt.index},
            )

    async def _resume_legacy_print(self, run: ActiveRun) -> None:
        task_id = run.entry.task_id
        try:
            streamed = await self.gateway.stream(
                ["resume", "--task-root", str(run.entry.task_root)],
                task_root=run.entry.task_root,
                timeout=None,
                task_id=task_id,
                cancel_key=f"legacy-resume:{task_id}",
            )
            result = streamed.result
            if result.code == 0 and result.data and result.data.get("ok"):
                run.phase = "done"
                self.repo.add_event(
                    "run.legacy_resumed",
                    f"{run.entry.task_name} 的旧版 print 会话已接回收尾",
                    actor="scheduler",
                    task_id=task_id,
                    details=result.data,
                )
            else:
                run.phase = "failed"
                detail = result.err.strip() or result.out.strip() or f"exit={result.code}"
                await self._record_run_failure(
                    task_id,
                    kind="run.incomplete",
                    detail=detail,
                    status={},
                )
        except Exception as exc:
            run.phase = "failed"
            await self._record_run_failure(
                task_id,
                kind="run.session_lost",
                detail=str(exc),
                status={"status": "aborted", "abort": {"reason": "session_lost", "detail": str(exc)}},
            )
        finally:
            self.active.pop(task_id, None)
            (self.paths.slots / f"{task_id}.json").unlink(missing_ok=True)
            if self.dispatcher is not None and self.dispatcher.v3_enabled:
                if run.phase == "done":
                    self.dispatcher.on_success(task_id, stage="running")
                self.dispatcher.release_container(task_id, success=run.phase == "done")
            try:
                await self.reconciler.run()
            except Exception:
                LOGGER.exception("legacy resume reconcile failed")
            self.bus.publish("queue.updated", self.queue_snapshot())
            self.bus.publish("task.updated", {"task_id": task_id, "phase": run.phase})

    def _write_session_shadow(self, task_id: str, status: dict[str, Any]) -> None:
        self.paths.sessions.mkdir(parents=True, exist_ok=True)
        path = self.paths.sessions / f"{task_id}.json"
        payload = {
            "taskId": task_id,
            "updatedAt": datetime.now().astimezone().replace(microsecond=0).isoformat(),
            **status,
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)

    def _sync_session_state(
        self,
        task_id: str,
        run: ActiveRun,
        status: dict[str, Any],
        seen_events: set[str],
    ) -> None:
        raw_rounds = status.get("rounds")
        rounds: list[dict[str, Any]] = (
            [item for item in raw_rounds if isinstance(item, dict)]
            if isinstance(raw_rounds, list)
            else []
        )
        attempt = int(status.get("attempt") or run.attempt or 0)
        if attempt:
            self.repo.replace_session_rounds(task_id, attempt, rounds)
            final_status = str(status.get("status") or "")
            outcome = (
                "accepted"
                if final_status == "accepted"
                else "aborted"
                if final_status in {"aborted", "session_lost"}
                else "running"
            )
            if final_status == "accepted":
                self.repo.set_job_status(task_id, "ran")
            elif final_status not in {"aborted", "session_lost"}:
                self.repo.set_job_status(task_id, "running")
            last_kind = str(status.get("lastKind") or "")
            abort_reason = str((status.get("abort") or {}).get("reason") or "")
            self.repo.execute(
                """
                INSERT INTO attempts(
                  task_id, seq, mode, container, started_at, finished_at, exit_code,
                  outcome, error_kind, detail, session_run_status, rounds, continues,
                  start_failed, interrupt_kind, last_activity_at
                ) VALUES(?, ?, 'supervised', ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                ON CONFLICT(task_id, seq) DO UPDATE SET
                  container=excluded.container, finished_at=excluded.finished_at,
                  outcome=excluded.outcome, error_kind=excluded.error_kind,
                  detail=excluded.detail, session_run_status=excluded.session_run_status,
                  rounds=excluded.rounds, continues=excluded.continues,
                  interrupt_kind=excluded.interrupt_kind,
                  last_activity_at=excluded.last_activity_at
                """,
                (
                    task_id,
                    attempt,
                    str(status.get("container") or ""),
                    str(status.get("startedAt") or started_iso(run)),
                    str(status.get("lastActivityAt") or "") if outcome != "running" else None,
                    outcome,
                    (abort_reason or last_kind) if outcome == "aborted" else None,
                    str((status.get("abort") or {}).get("detail") or ""),
                    final_status,
                    len(rounds),
                    int((status.get("continues") or {}).get("used") or 0),
                    last_kind,
                    str(status.get("lastActivityAt") or ""),
                ),
            )
        if self.dispatcher is not None:
            self.dispatcher.queue.update(
                task_id,
                stage="running",
                session_state=str(status.get("sessionState") or ""),
                continue_used=int((status.get("continues") or {}).get("used") or 0),
                next_action_at=str(status.get("nextActionAt") or ""),
                container_seconds=max(0, int(run.started_at and time.time() - run.started_at)),
            )
            self.dispatcher._publish()
        for event in status.get("logTail") or []:
            if not isinstance(event, dict):
                continue
            key = f"{event.get('at')}:{event.get('event')}:{event.get('round')}:{event.get('no')}"
            if key in seen_events:
                continue
            seen_events.add(key)
            line = json.dumps(event, ensure_ascii=False)
            run.log_lines.append(line)
            run.log_lines = run.log_lines[-500:]
            self.bus.publish(
                "run.log",
                {"task_id": task_id, "line": line, "kind": "supervisor", "batch": True},
            )
            action = str(event.get("event") or "")
            mapping = {
                "round.end": "session.round_end",
                "continue.sent": "session.continue_sent",
                "hold.start": "session.held",
                "session.aborted": "session.aborted",
                "session.accepted": "session.accepted",
                "prompt.delivery_retry": "session.prompt_retry",
                "prompt.sent": "session.prompt_sent",
                "prompt.delivery_failed": "session.prompt_failed",
            }
            if action in mapping:
                self.bus.publish(
                    mapping[action],
                    {
                        "task_id": task_id,
                        "round": event.get("round"),
                        "kind": event.get("kind") or event.get("reason"),
                        "continue": event.get("no"),
                        "detail": event,
                    },
                )

    async def _record_run_failure(
        self,
        task_id: str,
        *,
        kind: str,
        detail: str,
        status: dict[str, Any],
    ) -> None:
        self.repo.add_event(
            "run.failed",
            detail,
            level="error",
            actor="scheduler",
            task_id=task_id,
            details={"kind": kind, "session": status},
        )
        if self.dispatcher is not None and self.dispatcher.v3_enabled:
            _, decision, _ = await self.dispatcher.on_failure(
                task_id,
                Evidence(
                    stage="running",
                    exit_code=2,
                    skill_json={"failure_kind": kind, **status},
                    skill_stderr=detail,
                    trace_tail="",
                ),
                actor="scheduler",
            )
            if kind == "run.start_failed" and decision.action == "quarantine":
                self.repo.set_job_status(
                    task_id,
                    "start_failed",
                    notes="启动失败次数已用尽；可人工重新排队，不计入工程冷却",
                )
            elif kind == "run.start_failed" and decision.action == "retry":
                retry = asyncio.create_task(
                    self._retry_start_failed(task_id, max(30, decision.delay_seconds))
                )
                self._retry_tasks.add(retry)
                retry.add_done_callback(self._retry_tasks.discard)
        terminal = str(status.get("status") or "") == "aborted" or kind in {
            "run.continues_exhausted",
            "run.timeout",
            "run.no_progress",
            "run.hold_timeout",
            "run.unresponsive",
            "run.incomplete",
            "run.session_lost",
        }
        if terminal:
            reason = kind.removeprefix("run.")
            self.repo.set_job_status(task_id, "aborted", notes=detail)
            if self.ledger is not None:
                self.ledger.mark_outcome(task_id, "void")
            if self.intake is not None:
                try:
                    await self.intake.terminal_session(task_id, reason=reason)
                except Exception:
                    LOGGER.exception("terminal session release failed: %s", task_id)
        self.bus.publish(
            "toast",
            {"level": "error", "message": detail[:500], "task_id": task_id},
        )

    async def _retry_start_failed(self, task_id: str, delay: int) -> None:
        await asyncio.sleep(delay)
        if self._stopping or task_id in self.active:
            return
        task = self.repo.get_task(task_id)
        if task is None:
            return
        envelope = load_state(Path(str(task["task_root"])))
        if envelope.state is None or envelope.state.status not in {"prompt_ready", "run_failed"}:
            return
        try:
            await self.enqueue(task_id)
            self.repo.add_event(
                "run.start_retry",
                "启动失败未消耗会话机会，已重新排队",
                actor="scheduler",
                task_id=task_id,
            )
        except ApiError as exc:
            self.repo.add_event(
                "run.start_retry_skipped",
                exc.message,
                level="warn",
                actor="scheduler",
                task_id=task_id,
            )

    async def _trip_auth_breaker(self, failed_task_id: str, detail: str) -> None:
        if self.dispatcher is not None and self.dispatcher.v3_enabled:
            self.dispatcher.register_hold(
                source="breaker",
                scope=["global"],
                reason_code="gateway.auth",
                reason=detail,
                auto=False,
            )
            return
        waiting = list(self.queue)
        self.queue.clear()
        for entry in waiting:
            self.repo.set_job_status(entry.task_id, "blocked", notes="鉴权失败，队列已熔断")
            self.repo.add_event(
                "run.auth_breaker",
                "网关鉴权失败，任务保持阻塞",
                level="error",
                actor="scheduler",
                task_id=entry.task_id,
                details={"source_task": failed_task_id, "error": detail},
            )
        self.bus.publish(
            "system",
            {"level": "error", "message": "网关鉴权失败，整个运行队列已停止", "detail": detail},
        )

    async def _maintenance_loop(self) -> None:
        while True:
            await asyncio.sleep(30)
            await self._adopt_legacy_print_runs()
            if self.dispatcher is not None and self.dispatcher.v3_enabled:
                snapshot = await self.dispatcher.facts.snapshot(force=True)
                containers = set(snapshot.docker.containers)
            else:
                containers = await _running_containers()
            self._clean_slot_files(containers)

    def _clean_slot_files(self, containers: set[str]) -> None:
        for path in self.paths.slots.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                path.unlink(missing_ok=True)
                continue
            task_id = str(data.get("taskId") or "")
            container = str(data.get("container") or "")
            if task_id in self.active:
                try:
                    data["container"] = _container_for_state(self.active[task_id].entry.task_root)
                    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                except OSError:
                    pass
                continue
            if container and container not in containers:
                path.unlink(missing_ok=True)

    def _write_slot(self, entry: QueueEntry, phase: str) -> None:
        self.paths.slots.mkdir(parents=True, exist_ok=True)
        container = _container_for_state(entry.task_root)
        payload = {
            "taskId": entry.task_id,
            "pid": None,
            "container": container,
            "phase": phase,
            "startedAt": datetime.now().astimezone().replace(microsecond=0).isoformat(),
        }
        path = self.paths.slots / f"{entry.task_id}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _container_for_state(task_root: Path) -> str:
    envelope = load_state(task_root)
    if envelope.state is None:
        return ""
    if envelope.state.pendingContainer:
        return envelope.state.pendingContainer
    for attempt in reversed(envelope.state.attempts):
        if attempt.container and attempt.status in {"running", "pending_collect"}:
            return attempt.container
    return ""


def _phase_from_line(line: str) -> str:
    if "拉取镜像" in line:
        return "pulling"
    if "容器已达上限" in line:
        return "waiting"
    if "容器" in line and "运行中" in line:
        return "running"
    if "不可用" in line:
        return "verifying"
    if "导出轨迹" in line:
        return "collecting"
    if "提交" in line and "产物快照" in line:
        return "snapshotting"
    return ""


def _attempt_from_line(line: str) -> int:
    import re

    match = re.search(r"\[attempt\s+(\d+)\]", line)
    return int(match.group(1)) if match else 0


def _classify_failure(detail: str) -> str:
    lowered = detail.lower()
    if "401" in detail or "authenticat" in lowered:
        return "auth"
    if "超时" in detail or "timeout" in lowered:
        return "timeout"
    if "docker" in lowered or "daemon" in lowered:
        return "host_restart"
    if "单轮" in detail or "stop_reason" in detail or "结构" in detail:
        return "structure"
    return "unknown"


def _run_failure_kind(reason: str) -> str:
    mapping = {
        "continues_exhausted": "run.continues_exhausted",
        "run_timeout": "run.timeout",
        "no_progress": "run.no_progress",
        "hold_timeout": "run.hold_timeout",
        "manual_timeout": "run.hold_timeout",
        "session_lost": "run.session_lost",
        "unresponsive": "run.unresponsive",
        "validation_failed": "run.incomplete",
        "print_validation_failed": "run.incomplete",
    }
    return mapping.get(str(reason), f"run.{reason}")


def _elapsed(started: str, finished: str) -> float:
    if not started:
        return 0
    try:
        start = datetime.fromisoformat(started.replace("Z", "+00:00"))
        end = datetime.fromisoformat(finished.replace("Z", "+00:00")) if finished else datetime.now(start.tzinfo)
        return round(max(0, (end - start).total_seconds()), 1)
    except ValueError:
        return 0


def started_iso(run: ActiveRun) -> str:
    return datetime.fromtimestamp(run.started_at).astimezone().replace(microsecond=0).isoformat()


def _tail_lines(path: Path, limit: int) -> list[str]:
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - 256 * 1024))
            data = handle.read()
    except OSError:
        return []
    return data.decode("utf-8", errors="replace").splitlines()[-limit:]


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


async def _running_containers() -> set[str]:
    return await docker_running_containers(managed_only=True) or set()


async def _docker_ok() -> bool:
    return await docker_info_ok()
