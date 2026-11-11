"""Container-aware task queue and asynchronous run lifecycle."""

from __future__ import annotations

import asyncio
import json
import logging
import time
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
    ) -> None:
        self.paths = paths
        self.gateway = gateway
        self.repo = repo
        self.reconciler = reconciler
        self.bus = bus
        self.dispatcher = dispatcher
        self.attempt_total = max(1, attempt_total)
        self.queue: list[QueueEntry] = []
        self.active: dict[str, ActiveRun] = {}
        self.limit = 4
        self._tick_task: asyncio.Task[None] | None = None
        self._maintenance_task: asyncio.Task[None] | None = None
        self._stopping = False
        self._last_log_publish: dict[str, float] = {}

    async def start(self) -> None:
        if self.dispatcher is not None and self.dispatcher.v3_enabled:
            self.limit = self.dispatcher.effective_container_limit
        else:
            await self.refresh_limit()
        self._tick_task = asyncio.create_task(self._tick_loop())
        self._maintenance_task = asyncio.create_task(self._maintenance_loop())

    async def stop(self) -> None:
        self._stopping = True
        for task in (self._tick_task, self._maintenance_task):
            if task:
                task.cancel()
        await asyncio.gather(
            *(task for task in (self._tick_task, self._maintenance_task) if task),
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
        cancelled = await self.gateway.cancel(task_id)
        if active.task and active.task.done() and not cancelled:
            return {"state": "finished", "task_id": task_id}
        if active.task and not force:
            active.task.cancel()
        elif active.task:
            active.task.cancel()
            if cancelled:
                await self.gateway.cancel(task_id)
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
            return {
                "state": "running",
                "attempt_seq": active.attempt,
                "attempt_total": self.attempt_total,
                "phase": active.phase,
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
        if task_root is not None:
            envelope = load_state(task_root)
            if envelope.state is not None:
                state = envelope.state
                attempt_seq = len(state.attempts)
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
        last_phase = ""

        async def on_event(event: StreamEvent) -> None:
            run.log_lines.append(event.line)
            run.log_lines = run.log_lines[-500:]
            phase = _phase_from_line(event.line)
            if phase:
                run.phase = phase
                if phase == "running":
                    attempt = _attempt_from_line(event.line)
                    if attempt:
                        run.attempt = attempt
            now = time.monotonic()
            if now - self._last_log_publish.get(task_id, 0) >= 0.2:
                self._last_log_publish[task_id] = now
                self.bus.publish(
                    "run.log",
                    {"task_id": task_id, "line": event.line, "kind": event.kind, "batch": True},
                )

        try:
            streamed = await self.gateway.stream(
                ["run", "--task-root", str(run.entry.task_root)],
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
                self.bus.publish(
                    "toast", {"level": "info", "message": f"{run.entry.task_name} 运行结束", "task_id": task_id}
                )
            else:
                run.phase = "failed"
                detail = result.err.strip() or result.out.strip() or f"exit={result.code}"
                error_kind = _classify_failure(detail)
                self.repo.add_event(
                    "run.failed",
                    detail,
                    level="error",
                    actor="scheduler",
                    task_id=task_id,
                    details={"kind": error_kind, "exit_code": result.code},
                )
                if error_kind == "auth":
                    await self._trip_auth_breaker(task_id, detail)
                if self.dispatcher is not None and self.dispatcher.v3_enabled:
                    await self.dispatcher.on_failure(
                        task_id,
                        Evidence(
                            stage="running",
                            exit_code=result.code,
                            skill_json=result.data if isinstance(result.data, dict) else {},
                            skill_stderr=detail,
                            trace_tail=result.out[-8000:] if result.out else "",
                        ),
                        actor="scheduler",
                    )
                self.bus.publish(
                    "toast",
                    {"level": "error", "message": detail[:500], "task_id": task_id},
                )
        except asyncio.CancelledError:
            run.phase = "failed"
            self.repo.add_event("run.stopped", "运行已由用户停止", level="warn", actor="user", task_id=task_id)
            raise
        except Exception as exc:
            run.phase = "failed"
            LOGGER.exception("run task %s failed", task_id)
            self.repo.add_event(
                "run.failed",
                str(exc),
                level="error",
                actor="gateway",
                task_id=task_id,
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
            if last_phase != run.phase:
                self._last_log_publish.pop(task_id, None)

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


def _elapsed(started: str, finished: str) -> float:
    if not started:
        return 0
    try:
        start = datetime.fromisoformat(started.replace("Z", "+00:00"))
        end = datetime.fromisoformat(finished.replace("Z", "+00:00")) if finished else datetime.now(start.tzinfo)
        return round(max(0, (end - start).total_seconds()), 1)
    except ValueError:
        return 0


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


async def _running_containers() -> set[str]:
    return await docker_running_containers(managed_only=True) or set()


async def _docker_ok() -> bool:
    return await docker_info_ok()
