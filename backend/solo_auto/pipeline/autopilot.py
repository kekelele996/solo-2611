"""Unattended delivery: drive each task from authoring to the exported sheet.

The platform itself still writes neither prompts nor Feedback. It launches an
external agent (Codex CLI with the solo-2611 skill) for the two authoring
stages, runs the single turn through the regular scheduler, and derives every
next step from the facts on disk, so a restart resumes where the files say.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import signal
import time
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from solo_auto.dispatch.classify import Evidence
from solo_auto.errors import ApiError
from solo_auto.events.bus import EventBus
from solo_auto.gateway.skill import SkillGateway
from solo_auto.pipeline.agent_stream import AgentLogStreamer, read_log, usage
from solo_auto.pipeline.breaker import Breaker
from solo_auto.pipeline.desktop_chatgpt import (
    ChatGPTDesktop,
    RolloutIndex,
    RolloutInfo,
    read_rollout,
    rollout_delta,
    rollout_last_assistant,
    thread_id_from_rollout,
)
from solo_auto.pipeline.export_service import ExportService
from solo_auto.pipeline.feedback_service import FeedbackService
from solo_auto.pipeline.notify import Notifier
from solo_auto.pipeline.prompt_history import PromptHistory
from solo_auto.pipeline.prompt_templates import PromptTemplates
from solo_auto.pipeline.reaper import reap_and_record, reaped_note
from solo_auto.pipeline.reconcile import Reconciler
from solo_auto.pipeline.scheduler import RunScheduler
from solo_auto.pipeline.steps import evaluate_steps
from solo_auto.settings import AutopilotSettings, Paths
from solo_auto.store.repo import Repository
from solo_auto.store.state import load_state

if TYPE_CHECKING:
    from solo_auto.dispatch.kernel import Dispatcher

LOGGER = logging.getLogger(__name__)

ACTOR = "autopilot"
ACTIVE_STAGES = ("pending", "chatgpt", "authoring", "running", "delivering", "exporting")
AGENT_STAGES = ("authoring", "delivering")
STAGE_LABELS = {
    "pending": "等待调度",
    "chatgpt": "ChatGPT 桌面全流程",
    "authoring": "Agent 出题",
    "running": "容器单轮执行",
    "delivering": "Agent 复核、录屏与 Feedback",
    "exporting": "自动确认并导出",
    "awaiting_audit": "等待人工抽检",
    "done": "已交付",
    "failed": "失败",
    "stopped": "已停止",
}
AGENT_FLAGS = (
    "exec",
    "--json",
    "--skip-git-repo-check",
    "--dangerously-bypass-approvals-and-sandbox",
)
VERDICT = "AUTOPILOT:"
# 桌面端偶尔会先出现窗口、后拿到首条消息。先做两次低风险补按发送，
# 仍未产生带唯一标识的 rollout 才交给调度器重开会话。
CHATGPT_SUBMIT_RECOVERY_AT_SECONDS = (20, 45)
CHATGPT_LAUNCH_TIMEOUT_SECONDS = 90
LIVE_REFRESH_SECONDS = 10.0
USAGE_LIMIT_RE = re.compile(
    r"(?i)usage limit|rate.?limit|too many requests|\b429\b|quota|用量(受限|上限)|额度(不足|用尽)|超限"
)
PROGRESS_SKIP_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "coverage",
    "dist",
    "node_modules",
    "target",
}


def _now() -> str:
    return datetime.now().astimezone().replace(microsecond=0).isoformat()


def _meaningful_progress_at(root: Path) -> datetime | None:
    """Return the latest result-bearing activity, excluding agent chatter.

    Rollout files grow while the model emits reasoning tokens. They therefore
    cannot prove that a task is making useful progress. Files and gate markers
    under the task root can: source edits, state transitions, traces, delivery
    artifacts, review evidence, and recordings are all durable outcomes.
    """
    latest = 0.0
    exact_files = (
        root / "monitor" / "state.json",
        root / "monitor" / "feedback-check.json",
        root / "monitor" / "feedback-evidence.json",
        root / "monitor" / "runtime-verification.json",
        root / "delivery" / "feedback.md",
        root / "delivery" / "交付表.xlsx",
    )
    for path in exact_files:
        with suppress(OSError):
            latest = max(latest, path.stat().st_mtime)
    scan_roots = (
        root / "monitor" / "runs",
        root / "monitor" / "verification",
        root / "traces",
        root / "delivery",
        root / "recording",
        root / "workspace",
    )
    for scan_root in scan_roots:
        if not scan_root.exists():
            continue
        stack = [scan_root]
        while stack:
            directory = stack.pop()
            try:
                entries = list(os.scandir(directory))
            except OSError:
                continue
            for entry in entries:
                if entry.is_dir(follow_symlinks=False):
                    if entry.name not in PROGRESS_SKIP_DIRS:
                        stack.append(Path(entry.path))
                    continue
                try:
                    latest = max(latest, entry.stat(follow_symlinks=False).st_mtime)
                except OSError:
                    continue
    return datetime.fromtimestamp(latest).astimezone() if latest else None


def default_agent_command() -> list[str]:
    binary = (
        os.environ.get("SOLO_AUTO_AGENT_BIN") or shutil.which("codex") or str(Path.home() / ".local" / "bin" / "codex")
    )
    return [binary, *AGENT_FLAGS]


@dataclass(slots=True)
class Job:
    task_id: str
    task_name: str
    stage: str = "pending"
    detail: str = ""
    attempts: dict[str, int] = field(default_factory=dict)
    log: str = ""
    started_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    finished_at: str = ""
    failure_kind: str = ""
    stage_started_at: str = field(default_factory=_now)
    audit: dict[str, Any] = field(default_factory=dict)
    redo_feedback: bool = False
    reconfirm_required: bool = False
    # 人工确认"失败就失败了"：不再提醒，也不会被继续计划重新拉起；手动重试会清除。
    ignored: bool = False
    agent_pid: int = 0
    desktop_running: bool = False
    desktop_marker: str = ""
    desktop_prompt_file: str = ""
    desktop_workdir: str = ""
    desktop_deep_link: str = ""
    desktop_rollout: str = ""
    desktop_thread_id: str = ""
    desktop_started_at: str = ""
    desktop_last_progress_at: str = ""
    desktop_stage_id: int = 0
    desktop_recovery_attempts: int = 0
    desktop_recovery_last_at: str = ""
    desktop_recovery_error: str = ""

    @property
    def active(self) -> bool:
        return self.stage in ACTIVE_STAGES


@dataclass(slots=True)
class AgentRun:
    stage: str
    process: asyncio.subprocess.Process
    waiter: asyncio.Task[None]
    log: Path
    stage_id: int = 0
    streamer: asyncio.Task[None] | None = None
    done: asyncio.Event = field(default_factory=asyncio.Event)


class Autopilot:
    def __init__(
        self,
        *,
        paths: Paths,
        skill_root: Path,
        repo: Repository,
        reconciler: Reconciler,
        bus: EventBus,
        scheduler: RunScheduler,
        feedback: FeedbackService,
        exports: ExportService,
        settings: AutopilotSettings,
        gateway: SkillGateway,
        dispatcher: Dispatcher | None = None,
    ) -> None:
        self.paths = paths
        self.skill_root = skill_root
        self.repo = repo
        self.reconciler = reconciler
        self.bus = bus
        self.scheduler = scheduler
        self.feedback = feedback
        self.exports = exports
        self.gateway = gateway
        self.dispatcher = dispatcher
        self.settings = settings
        self.usage_limited_until = 0.0
        self.usage_limit_reason = ""
        self.breaker =Breaker(paths.home / "breaker.json", bus, settings)
        self.prompt_templates = PromptTemplates(paths)
        self.prompt_history = PromptHistory(repo, paths.home / "prompt-history.jsonl")
        self.notifier: Notifier | None = None
        self._agent_command_override: list[str] | None = None
        self.audit_service: Any = None
        self.ledger: Any = None
        self.apply_settings(settings)
        self.tick_seconds = 3.0
        self.state_file = paths.home / "autopilot.json"
        self.log_root = paths.logs / "autopilot"
        self.jobs: dict[str, Job] = {}
        self.agents: dict[str, AgentRun] = {}
        self.desktop_followers: dict[str, asyncio.Task[None]] = {}
        self.desktop_terminal_seen: set[str] = set()
        self._loop_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self.paused = False
        self._load()

    def apply_settings(self, settings: AutopilotSettings) -> None:
        self.settings = settings
        self.breaker.settings = settings
        self.agent_timeout_seconds = settings.stage_timeout_seconds
        self.stage_attempts = settings.stage_attempts
        self.run_attempts = settings.run_attempts
        desktop = getattr(self, "desktop", None)
        if desktop is None or getattr(self, "_desktop_helper", None) != settings.chatgpt_push_helper:
            self.desktop = ChatGPTDesktop(home=self.paths.home, helper=settings.chatgpt_push_helper)
            self._desktop_helper = settings.chatgpt_push_helper
        rollouts = getattr(self, "rollouts", None)
        if rollouts is None or rollouts.sessions_dir != self.desktop.sessions_dir:
            self.rollouts = RolloutIndex(self.desktop.sessions_dir)
            self._rollouts_refreshed = 0.0
        self.parallel = {
            "authoring": settings.parallel_authoring,
            "delivering": settings.parallel_delivering,
        }

    def attach_audit(self, service: Any) -> None:
        self.audit_service = service

    def attach_ledger(self, ledger: Any) -> None:
        self.ledger = ledger

    def attach_notifier(self, notifier: Notifier) -> None:
        self.notifier = notifier

    @property
    def agent_command(self) -> list[str]:
        if self._agent_command_override is not None:
            return self._agent_command_override
        base = [self.settings.agent_bin] if self.settings.agent_bin else default_agent_command()
        if self.settings.restricted:
            base = [part for part in base if part not in {"--dangerously-bypass-approvals-and-sandbox"}]
            base.extend(["--sandbox", "workspace-write"])
        return [*base, *self.settings.agent_args]

    @agent_command.setter
    def agent_command(self, value: list[str]) -> None:
        self._agent_command_override = value

    @property
    def chatgpt_parallel(self) -> int:
        """One desktop session spans authoring through delivery, so both pools share one limit."""
        if self.dispatcher is not None and self.dispatcher.v3_enabled:
            return self.dispatcher.effective_session_limit
        return max(1, self.settings.parallel_authoring + self.settings.parallel_delivering)

    # ---- live ChatGPT sessions -------------------------------------

    async def refresh_live_sessions(self, *, force: bool = False) -> None:
        """Re-read rollout files so capacity reflects threads that are really running."""
        now = time.monotonic()
        if not force and now - self._rollouts_refreshed < LIVE_REFRESH_SECONDS:
            return
        self._rollouts_refreshed = now
        window = float(max(self.settings.orphan_quiet_seconds, CHATGPT_LAUNCH_TIMEOUT_SECONDS))
        tracked = [
            Path(job.desktop_rollout)
            for job in self.jobs.values()
            if job.desktop_running and job.desktop_rollout
        ]

        def work() -> None:
            self.rollouts.refresh(window_seconds=window)
            for path in tracked:
                self.rollouts.update(path)

        await asyncio.to_thread(work)
        self._detect_usage_limit()

    def orphan_sessions(self) -> list[RolloutInfo]:
        """Threads the platform launched but no longer follows, which still consume quota."""
        tracked = {job.desktop_marker for job in self.jobs.values() if job.desktop_running and job.desktop_marker}
        return [
            info
            for info in self.rollouts.live(quiet_seconds=self.settings.orphan_quiet_seconds)
            if info.marker not in tracked
        ]

    @property
    def orphan_session_keys(self) -> set[str]:
        return {f"orphan:{info.marker}" for info in self.orphan_sessions()}

    def occupied_sessions(self) -> tuple[int, int]:
        """Return (sessions really running, of which no longer followed by the platform)."""
        tracked = sum(1 for job in self.jobs.values() if job.desktop_running)
        orphans = len(self.orphan_sessions())
        return tracked + orphans, orphans

    def usage_limited(self) -> bool:
        return time.time() < self.usage_limited_until

    def _detect_usage_limit(self) -> None:
        now = time.time()
        cooldown = self.settings.usage_limit_cooldown_seconds
        for info in self.rollouts.snapshot():
            if not info.marker or not info.error_text or now - info.mtime > cooldown:
                continue
            if not USAGE_LIMIT_RE.search(info.error_text):
                continue
            until = info.mtime + cooldown
            if until <= self.usage_limited_until:
                continue
            first = not self.usage_limited()
            self.usage_limited_until = until
            self.usage_limit_reason = info.error_text[:300]
            if first:
                self.repo.add_event(
                    "autopilot.chatgpt.usage_limited",
                    f"ChatGPT 会话触发用量限制，{cooldown // 60} 分钟内不再新开会话",
                    level="warn",
                    actor=ACTOR,
                    task_id=info.task_id or None,
                    details={"rollout": str(info.path), "error": info.error_text[:500]},
                )

    # ---- lifecycle -------------------------------------------------

    async def start(self) -> None:
        with suppress(Exception):
            self.prompt_history.backfill()
        if not self.settings.enabled:
            return
        if self._loop_task is None:
            self._loop_task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._loop_task is not None:
            self._loop_task.cancel()
            await asyncio.gather(self._loop_task, return_exceptions=True)
            self._loop_task = None
        for task_id in list(self.agents):
            await self._kill_agent(task_id)
        for task in list(self.desktop_followers.values()):
            task.cancel()
        await asyncio.gather(*self.desktop_followers.values(), return_exceptions=True)
        self.desktop_followers.clear()

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                LOGGER.exception("autopilot tick failed")
            await asyncio.sleep(self.tick_seconds)

    # ---- public API ------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        jobs = sorted(self.jobs.values(), key=lambda job: job.started_at, reverse=True)
        return {
            "enabled": self.settings.enabled,
            "agent_mode": self.settings.agent_mode,
            "chatgpt_available": self.desktop.available(),
            "paused": self.paused or self.breaker.tripped,
            "paused_by_breaker": self.breaker.tripped,
            "breaker": self.breaker.state,
            "agent_command": self.agent_command[0],
            "agent_available": self.desktop.available()
            if self.settings.agent_mode == "chatgpt"
            else bool(shutil.which(self.agent_command[0])),
            "agents_running": len(self.agents) + sum(job.desktop_running for job in jobs),
            "active": sum(1 for job in jobs if job.active),
            "parallel_authoring": self.settings.parallel_authoring,
            "parallel_delivering": self.settings.parallel_delivering,
            "parallel_chatgpt": self.chatgpt_parallel,
            "stage_timeout_seconds": self.settings.stage_timeout_seconds,
            "task_timeout_seconds": self.settings.task_timeout_seconds,
            "chatgpt_timeout_seconds": self.settings.chatgpt_timeout_seconds,
            "jobs": [self._job_view(job) for job in jobs],
        }

    def job(self, task_id: str) -> dict[str, Any] | None:
        job = self.jobs.get(task_id)
        return self._job_view(job) if job else None

    async def enroll(self, task_ids: list[str]) -> dict[str, Any]:
        async with self._lock:
            if self.breaker.tripped:
                raise ApiError(409, "AUTOPILOT_BREAKER_OPEN", "自动交付已熔断，请先恢复")
            self._require_executor()
            result = self._activate_tasks(task_ids, recover_terminal=True, actor="user")
        return {**result, **self.snapshot()}

    async def start_tasks(self, task_ids: list[str]) -> dict[str, Any]:
        """Start selected tasks and recover any interrupted or failed job."""
        async with self._lock:
            self._require_executor()
            self.paused = False
            self.breaker.reset()
            result = self._activate_tasks(task_ids, recover_terminal=True, actor="user")
            self._changed()
        return {**result, **self.snapshot()}

    def _require_executor(self) -> None:
        if not self.settings.enabled:
            raise ApiError(409, "AUTOPILOT_DISABLED", "自动交付总开关已关闭")
        if self.settings.agent_mode == "chatgpt":
            if not self.desktop.available():
                raise ApiError(409, "CHATGPT_APP_MISSING", "找不到 ChatGPT.app 或桌面深链接辅助程序")
        elif not shutil.which(self.agent_command[0]):
            raise ApiError(
                409,
                "AGENT_NOT_FOUND",
                f"找不到 Agent 命令：{self.agent_command[0]}。请安装 Codex CLI 或设置 SOLO_AUTO_AGENT_BIN",
            )

    def _activate_tasks(
        self,
        task_ids: list[str],
        *,
        recover_terminal: bool,
        actor: str,
    ) -> dict[str, Any]:
        unique_ids = list(dict.fromkeys(task_ids))
        tasks: dict[str, dict[str, Any]] = {}
        for task_id in unique_ids:
            task = self.repo.get_task(task_id)
            if not task:
                raise ApiError(404, "TASK_NOT_FOUND", f"任务不存在：{task_id}")
            tasks[task_id] = task

        added: list[str] = []
        recovered: list[str] = []
        already_active: list[str] = []
        skipped: list[dict[str, str]] = []
        changed = False
        for task_id, task in tasks.items():
            current = self.jobs.get(task_id)
            if current and current.active:
                already_active.append(task_id)
                continue
            if current and current.ignored and current.stage in {"failed", "stopped"}:
                skipped.append({"task_id": task_id, "reason": "失败已忽略，不再重新拉起"})
                continue
            if current and recover_terminal and current.stage in {"failed", "stopped"}:
                previous_stage = current.stage
                try:
                    target = self._reset_for_retry(current)
                except ApiError as exc:
                    skipped.append({"task_id": task_id, "reason": exc.message})
                    continue
                recovered.append(task_id)
                self.repo.add_event(
                    "autopilot.recovered",
                    f"{task['task_name']} 从「{STAGE_LABELS.get(target, target)}」重新拉起",
                    actor=actor,
                    task_id=task_id,
                    details={"from": previous_stage, "stage": target},
                )
                changed = True
                continue
            if current:
                skipped.append({"task_id": task_id, "reason": f"当前阶段不可恢复：{current.stage}"})
                continue
            if str(task.get("job_status") or "") in {"archived", "aborted"}:
                skipped.append({"task_id": task_id, "reason": "任务已结束，不能重新拉起"})
                continue
            self.jobs[task_id] = Job(task_id=task_id, task_name=str(task["task_name"]))
            added.append(task_id)
            self.repo.add_event(
                "autopilot.enrolled",
                f"{task['task_name']} 进入自动交付（无人工复核）",
                actor=actor,
                task_id=task_id,
            )
            changed = True
        if changed:
            self._changed()
        return {
            "added": added,
            "recovered": recovered,
            "already_active": already_active,
            "skipped": skipped,
        }

    async def cancel(
        self,
        task_ids: list[str] | None = None,
        *,
        kill_containers: bool | None = None,
    ) -> dict[str, Any]:
        targets = task_ids or [job.task_id for job in self.jobs.values() if job.active]
        stopped: list[str] = []
        should_kill = self.settings.stop_kills_containers if kill_containers is None else kill_containers
        for task_id in targets:
            job = self.jobs.get(task_id)
            if not job or not job.active:
                continue
            await self._kill_agent(task_id)
            await self._stop_desktop_follower(task_id)
            desktop_stopped = job.desktop_running
            job.desktop_running = False
            if desktop_stopped and self.dispatcher is not None and self.dispatcher.v3_enabled:
                self.dispatcher.release_session(task_id)
            cleanup_detail = ""
            if should_kill:
                task = self.repo.get_task(task_id)
                if task:
                    if task_id in self.scheduler.active or self.scheduler.queue_position(task_id) is not None:
                        with suppress(ApiError):
                            await self.scheduler.stop_task(task_id, force=True)
                    # 先结束会话遗留的运行进程，否则它会在清理后重新拉起容器。
                    reaped = await reap_and_record(self.repo, task["task_root"], task_id=task_id, actor=ACTOR)
                    cleanup = await self.gateway.call(
                        ["cleanup", "--task-root", str(task["task_root"]), "--force"],
                        timeout=120,
                        task_id=task_id,
                    )
                    cleanup_detail = reaped_note(reaped) + (
                        "；已中断容器"
                        if cleanup.code == 0
                        else f"；容器清理失败：{cleanup.err.strip() or cleanup.code}"
                    )
            chatgpt_detail = (
                "；平台已停止跟踪。ChatGPT 会话需要你在 ChatGPT 里手动停止，停止前它仍计入会话名额"
                if desktop_stopped
                else ""
            )
            self._finish(job, "stopped", "已手动停止" + cleanup_detail + chatgpt_detail)
            stopped.append(task_id)
        self._changed()
        return {"stopped": stopped, **self.snapshot()}

    def pause(self) -> dict[str, Any]:
        self.paused = True
        self._changed()
        return self.snapshot()

    async def resume(self, task_ids: list[str] | None = None) -> dict[str, Any]:
        """Resume the executor and restart interrupted jobs from disk facts."""
        targets = task_ids
        if targets is None:
            targets = [
                task_id
                for task_id, job in self.jobs.items()
                if job.stage not in {"done", "awaiting_audit"}
            ]
        return await self.start_tasks(targets)

    def mark_done(self, task_id: str, detail: str = "自动交付完成") -> None:
        job = self.jobs.get(task_id)
        if job:
            self._finish(job, "done", detail)

    def mark_failed(self, task_id: str, detail: str) -> None:
        job = self.jobs.get(task_id)
        if job:
            self._finish(job, "failed", detail)

    def _reset_for_retry(self, job: Job, from_stage: str = "") -> str:
        if job.active and job.stage != "failed":
            raise ApiError(409, "AUTOPILOT_ACTIVE", "自动交付仍在运行，先停止再重试")
        stages = self._retry_stages(job)
        if from_stage and from_stage not in stages:
            raise ApiError(409, "AUTOPILOT_STAGE_UNAVAILABLE", self._retry_reason(job, from_stage))
        target = from_stage or (stages[0] if stages else "")
        if not target:
            raise ApiError(409, "AUTOPILOT_NO_RETRY_STAGE", "磁盘事实不允许从任何阶段续跑")
        remove = False
        order = ["chatgpt", "authoring", "running", "delivering", "exporting"]
        for stage in order:
            if stage == target:
                remove = True
            if remove:
                job.attempts.pop(stage, None)
        job.stage = "pending"
        job.detail = f"从「{STAGE_LABELS.get(target, target)}」续跑"
        job.failure_kind = ""
        job.ignored = False
        job.finished_at = ""
        job.stage_started_at = _now()
        job.agent_pid = 0
        job.desktop_running = False
        self._changed()
        return target

    async def retry(self, task_id: str, *, from_stage: str = "") -> dict[str, Any]:
        job = self.jobs.get(task_id)
        if not job:
            raise ApiError(404, "AUTOPILOT_JOB_NOT_FOUND", "该任务没有自动交付记录")
        async with self._lock:
            self._reset_for_retry(job, from_stage)
        return self._job_view(job)

    def stages(self, task_id: str) -> list[dict[str, Any]]:
        if task_id not in self.jobs:
            raise ApiError(404, "AUTOPILOT_JOB_NOT_FOUND", "该任务没有自动交付记录")
        return self.repo.autopilot_stages(task_id)

    def ignore(self, task_id: str) -> bool:
        """Accept a failed job as final so nothing retries it automatically."""
        job = self.jobs.get(task_id)
        if job is None:
            return False
        if job.active:
            raise ApiError(409, "AUTOPILOT_ACTIVE", "自动交付仍在运行，不能忽略")
        job.ignored = True
        self._changed()
        return True

    def forget(self, task_id: str) -> None:
        """Drop the record of a task that has been removed from the platform."""
        if self.jobs.pop(task_id, None) is not None:
            self._changed()

    def log_tail(self, task_id: str, limit: int = 200, *, stage: str = "") -> dict[str, Any]:
        job = self.jobs.get(task_id)
        if not job:
            raise ApiError(404, "AUTOPILOT_JOB_NOT_FOUND", "该任务没有自动交付记录")
        path: Path | None = Path(job.desktop_rollout) if job.desktop_rollout else None
        if stage:
            row = next(
                (item for item in self.repo.autopilot_stages(task_id) if item.get("stage") == stage),
                None,
            )
            if row and row.get("log_path"):
                path = Path(str(row["log_path"]))
        if path is None and job.log:
            path = Path(job.log)
        if self.settings.agent_mode == "chatgpt" and path and path.is_file() and path.name.startswith("rollout-"):
            lines = read_rollout(path, limit)
        else:
            lines = read_log(path, limit) if path and path.is_file() else []
        return {"task_id": task_id, "stage": stage or job.stage, "file": str(path or ""), "lines": lines}

    async def open_chatgpt(self, task_id: str) -> dict[str, Any]:
        job = self.jobs.get(task_id)
        if not job:
            raise ApiError(404, "AUTOPILOT_JOB_NOT_FOUND", "该任务没有自动交付记录")
        target = f"codex://threads/{job.desktop_thread_id}" if job.desktop_thread_id else "-a"
        command = ["open", "-a", "ChatGPT"] if target == "-a" else ["open", target]
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()
        if process.returncode != 0:
            raise ApiError(
                500,
                "CHATGPT_OPEN_FAILED",
                stderr.decode("utf-8", errors="replace").strip() or "打开 ChatGPT 会话失败",
            )
        return {
            "ok": True,
            "thread_id": job.desktop_thread_id,
            "target": target,
        }

    # ---- state machine ---------------------------------------------

    async def tick(self) -> None:
        async with self._lock:
            if self.breaker.tripped and self.breaker.maybe_auto_reset():
                self.repo.add_event(
                    "autopilot.breaker.auto_reset",
                    f"熔断冷却结束，自动恢复（第 {self.breaker.auto_resets} 次）",
                    level="warn",
                    actor=ACTOR,
                )
            if self.settings.agent_mode == "chatgpt":
                try:
                    await self.refresh_live_sessions()
                except Exception:
                    LOGGER.exception("refresh live ChatGPT sessions failed")
            if not self.settings.enabled or self.paused or self.breaker.tripped:
                return
            for job in list(self.jobs.values()):
                if not job.active:
                    continue
                try:
                    if await self._enforce_task_timeout(job):
                        continue
                    await self._advance(job)
                except ApiError as error:
                    self._finish(job, "failed", f"{error.code}: {error.message}")
                except Exception as error:
                    LOGGER.exception("autopilot job failed: %s", job.task_id)
                    self._finish(job, "failed", f"{type(error).__name__}: {error}")

    async def _enforce_task_timeout(self, job: Job) -> bool:
        """Stop a job after its cumulative wall-clock budget expires.

        Desktop no-progress checks see activity timestamps, so a model that keeps
        emitting reasoning tokens can hold a container indefinitely. This check
        uses the immutable job start time and ignores activity, so retries cannot
        extend the total budget.
        """
        started = _parse_datetime(job.started_at)
        if started is None:
            return False
        elapsed = (datetime.now().astimezone() - started).total_seconds()
        if elapsed < self.settings.task_timeout_seconds:
            return False

        hours = self.settings.task_timeout_seconds / 3600
        detail = f"任务累计运行超过 {hours:g} 小时仍未完成，已终止并释放容器"
        await self._terminate_job(job, detail=detail, kind="agent.timeout")
        return True

    async def _terminate_job(self, job: Job, *, detail: str, kind: str) -> None:
        """Terminate and quarantine a job, always releasing its container."""
        await self._kill_agent(job.task_id)
        desktop_was_running = job.desktop_running
        if job.desktop_running or job.desktop_stage_id:
            await self._finish_desktop_stage(
                job,
                outcome="failed",
                kind=kind.replace(".", "_"),
                detail=detail,
            )
        else:
            await self._stop_desktop_follower(job.task_id)

        cleanup_detail = ""
        task = self.repo.get_task(job.task_id)
        if task:
            if job.task_id in self.scheduler.active or self.scheduler.queue_position(job.task_id) is not None:
                with suppress(ApiError):
                    await self.scheduler.stop_task(job.task_id, force=True)
            reaped = await reap_and_record(self.repo, task["task_root"], task_id=job.task_id, actor=ACTOR)
            cleanup = await self.gateway.call(
                ["cleanup", "--task-root", str(task["task_root"]), "--force"],
                timeout=120,
                task_id=job.task_id,
            )
            cleanup_detail = reaped_note(reaped) + (
                "；容器已释放"
                if cleanup.code == 0
                else f"；容器释放失败：{cleanup.err.strip() or cleanup.code}"
            )
        if desktop_was_running:
            cleanup_detail += "；ChatGPT 会话无法由平台关闭，它停止写入之前仍计入会话名额，不会因此多开新会话"
        job.failure_kind = kind
        job.ignored = True
        self._finish(job, "failed", detail + cleanup_detail)
        if self.dispatcher is not None and self.dispatcher.v3_enabled:
            self.dispatcher.on_job_finished(job.task_id, outcome="failed", detail=detail + cleanup_detail)

    async def _advance(self, job: Job) -> None:
        if job.task_id in self.agents:
            return
        task = self.repo.get_task(job.task_id)
        if not task:
            self._finish(job, "failed", "任务已不存在")
            return
        root = Path(task["task_root"])
        if self.settings.agent_mode == "chatgpt":
            await self._advance_chatgpt(job, task, root)
            return
        envelope = load_state(root)
        if not envelope.compatible or envelope.state is None:
            self._finish(job, "failed", envelope.error or "任务状态不可读")
            return
        state = envelope.state
        duplicates = self.repo.session_counts()
        final = evaluate_steps(root, duplicate_sessions=duplicates)
        if job.redo_feedback:
            await self._launch(job, "delivering", root, ["人工抽检打回：重写 Feedback 后重新检查"])
            return
        if final.complete and not job.reconfirm_required:
            await self._complete_or_audit(job, task, root)
            return
        if job.task_id in self.scheduler.active or self.scheduler.queue_position(job.task_id) is not None:
            self._stage(job, "running", "排队或运行中")
            return
        facts = evaluate_steps(root, duplicate_sessions=duplicates, require_review_ack=False)
        ok = [step.ok for step in facts.steps]

        if state.session is None:
            if state.status == "running":
                self._finish(job, "failed", "任务停在 running 但没有存活的运行实例，请在任务页处理后重试")
                return
            if state.status == "prepared" or not all(ok[:3]):
                if state.status not in {"prepared", "prompt_ready"}:
                    self._finish(job, "failed", f"当前状态 {state.status} 下提示词门禁未通过：{facts.missing[0]}")
                    return
                await self._launch(job, "authoring", root, facts.missing)
                return
            duplicate = self.prompt_history.duplicate(job.task_id, root)
            if duplicate is not None:
                await self._launch(job, "authoring", root, [f"{duplicate.reason}，请重新设计题面后再安装"])
                return
            if str(task.get("job_status")) == "blocked":
                self._finish(job, "failed", "任务当前处于阻断状态，请检查失败原因并在任务页确认后续处理")
                return
            if job.attempts.get("running", 0) >= self.run_attempts:
                self._finish(job, "failed", "容器单轮执行多次未得到结构完整的会话")
                return
            job.attempts["running"] = job.attempts.get("running", 0) + 1
            await self.scheduler.enqueue(job.task_id)
            self._stage(job, "running", f"已入队（第 {job.attempts['running']} 次）")
            return

        if not all(ok[:8]):
            self._finish(job, "failed", "运行后的快照或轨迹不完整：" + "；".join(facts.missing[:3]))
            return
        if not (ok[8] and ok[9]):
            await self._launch(job, "delivering", root, [item for item in facts.missing if item[:2] in {"9.", "10"}])
            return

        if self._reject_duplicate(job, root):
            return
        self._stage(job, "exporting", "机器门禁通过，自动确认并导出")
        if job.reconfirm_required or not final.steps[9].ok:
            check = await self.feedback.check(job.task_id)
            if not check["ok"]:
                reasons = [str(item) for item in check["errors"]] or ["Feedback 检查未通过"]
                await self._launch(job, "delivering", root, reasons)
                return
            self.feedback.ack(job.task_id, acked=True, actor=ACTOR)
            job.reconfirm_required = False
        result = await self.exports.export(task_ids=[job.task_id])
        if evaluate_steps(root, duplicate_sessions=self.repo.session_counts()).complete:
            await self._complete_or_audit(job, task, root)
            return
        reasons = [str(reason) for item in result.get("skipped") or [] for reason in item.get("reason") or []]
        self._finish(job, "failed", "导出被拒绝：" + ("；".join(reasons[:3]) or "原因未知"))

    async def _complete_or_audit(self, job: Job, task: dict[str, Any], root: Path) -> None:
        job.reconfirm_required = False
        self.prompt_history.remember(job.task_id, root)
        ack = self._read_json(root / "monitor" / "review-ack.json")
        if ack.get("ackedBy") == ACTOR and self.audit_service is not None:
            audit = self.audit_service.assess(job.task_id, str(task.get("task_type") or ""))
            job.audit = audit
            if audit.get("sampled") and not audit.get("verdict"):
                self._stage(job, "awaiting_audit", "自动交付完成，等待人工抽检")
                if self.ledger:
                    self.ledger.mark_delivered(job.task_id)
                return
            if audit.get("sampled") and audit.get("verdict") == "reject":
                self._finish(job, "failed", f"人工抽检打回：{audit.get('reason') or '未通过'}")
                return
        if self.ledger:
            self.ledger.mark_delivered(job.task_id)
        self._finish(job, "done", "十一项交付与抽检策略均已通过")

    def _reject_duplicate(self, job: Job, root: Path) -> bool:
        duplicate = self.prompt_history.duplicate(job.task_id, root)
        if duplicate is None:
            return False
        job.failure_kind = "prompt_duplicate"
        self._finish(job, "failed", f"{duplicate.reason}，不导出；工程归还后会换题重出")
        return True

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    # ---- ChatGPT desktop executor ----------------------------------

    async def _advance_chatgpt(self, job: Job, task: dict[str, Any], root: Path) -> None:
        envelope = load_state(root)
        if not envelope.compatible or envelope.state is None:
            self._finish(job, "failed", envelope.error or "任务状态不可读")
            return
        state = envelope.state
        facts = evaluate_steps(
            root,
            duplicate_sessions=self.repo.session_counts(),
            require_review_ack=False,
        )
        final = evaluate_steps(root, duplicate_sessions=self.repo.session_counts())
        if job.desktop_running:
            if not job.desktop_workdir:
                job.desktop_workdir = str(root)
            self._ensure_desktop_follower(job)
            info = self._tracked_rollout(job)
            progress = _meaningful_progress_at(root)
            if info is not None and info.last_tool_at:
                # 工具调用和命令输出说明会话在真实干活（例如轮询数小时的容器运行），
                # 只有纯推理、不调用工具的空转才算没有进展。
                tool_at = datetime.fromtimestamp(info.last_tool_at).astimezone()
                progress = max(progress, tool_at) if progress else tool_at
            if progress:
                previous = _parse_datetime(job.desktop_last_progress_at)
                if previous is None or progress > previous:
                    job.desktop_last_progress_at = progress.replace(microsecond=0).isoformat()
                    job.updated_at = _now()
                    self._changed()
            rollout = Path(job.desktop_rollout) if job.desktop_rollout else None
            terminal = info.turn_state if info is not None and info.terminal else ""
            if all(step.ok for step in facts.steps[:10]):
                await self._finish_desktop_stage(job, outcome="ok", detail="ChatGPT 桌面会话完成前 10 项")
                await self._export_chatgpt(job, task, root)
                return
            if terminal in {"turn_aborted", "stream_error", "error", "task_complete"}:
                if terminal == "task_complete" and job.task_id not in self.desktop_terminal_seen:
                    self.desktop_terminal_seen.add(job.task_id)
                    self._stage(job, "chatgpt", "ChatGPT 回合已结束，等待磁盘门禁稳定")
                    return
                final_text = rollout_last_assistant(rollout) if rollout and rollout.is_file() else ""
                error_text = info.error_text if info is not None and terminal != "task_complete" else ""
                kind = (
                    "agent_blocked"
                    if state.status == "run_failed" or "AUTOPILOT: BLOCKED" in final_text
                    else "agent_reported"
                    if terminal == "task_complete"
                    else "usage_limited"
                    if USAGE_LIMIT_RE.search(error_text)
                    else self._failure_kind("chatgpt", "\n".join(part for part in (error_text, final_text) if part), 1)
                )
                detail = (
                    f"ChatGPT 会话报错：{error_text}" if kind == "usage_limited" else final_text or error_text
                ) or f"ChatGPT 会话终止：{terminal}"
                await self._finish_desktop_stage(job, outcome="failed", kind=kind, detail=detail)
                self._finish(job, "failed", f"ChatGPT 会话未完成前 10 项：{detail[:300]}")
                return
            started = _parse_datetime(job.desktop_started_at) or _parse_datetime(job.stage_started_at)
            last_progress = _parse_datetime(job.desktop_last_progress_at) or started
            now = datetime.now().astimezone()
            if last_progress and (now - last_progress).total_seconds() >= self.settings.chatgpt_timeout_seconds:
                minutes = max(1, self.settings.chatgpt_timeout_seconds // 60)
                detail = f"任务连续 {minutes} 分钟没有有效进展，已终止并释放容器"
                await self._terminate_job(
                    job,
                    detail=detail,
                    kind="agent.no_progress",
                )
                return
            elapsed = int((now - started).total_seconds()) if started else 0
            self._stage(job, "chatgpt", f"ChatGPT 桌面会话运行中（{elapsed // 60} 分钟）")
            return
        if final.complete and not job.reconfirm_required:
            await self._complete_or_audit(job, task, root)
            return
        if all(step.ok for step in facts.steps[:10]):
            await self._export_chatgpt(job, task, root)
            return
        await self._launch_chatgpt(job, root, facts.missing)

    def _tracked_rollout(self, job: Job) -> RolloutInfo | None:
        if job.desktop_rollout:
            path = Path(job.desktop_rollout)
            return self.rollouts.update(path) if path.is_file() else None
        info = self.rollouts.find(job.desktop_marker) if job.desktop_marker else None
        if info is not None:
            job.desktop_rollout = str(info.path)
            job.desktop_thread_id = thread_id_from_rollout(info.path)
            job.log = str(info.path)
            self._changed()
        return info

    async def _session_slot(self, job: Job) -> tuple[bool, str]:
        """Decide whether one more desktop thread may start, counting threads that are really alive."""
        if self.usage_limited():
            minutes = max(1, int((self.usage_limited_until - time.time()) // 60) + 1)
            return False, f"ChatGPT 触发用量限制，约 {minutes} 分钟后再新开会话"
        occupied, orphans = self.occupied_sessions()
        limit = self.chatgpt_parallel
        if occupied >= limit:
            extra = f"，其中 {orphans} 个已脱离平台跟踪但仍在运行" if orphans else ""
            return False, f"等待 ChatGPT 会话空位（实际运行 {occupied}/{limit}{extra}）"
        if self.dispatcher is not None and self.dispatcher.v3_enabled:
            decision = await self.dispatcher.request_session(job.task_id, stage="chatgpt")
            if not decision.allowed:
                return False, decision.text or "等待会话空位"
        return True, ""

    def _adopt_live_session(self, job: Job) -> bool:
        """Re-attach a thread that is still running for this task instead of opening a second one."""
        live = [info for info in self.orphan_sessions() if info.task_id == job.task_id]
        if not live:
            return False
        info = max(live, key=lambda item: item.mtime)
        now = _now()
        job.desktop_stage_id = self.repo.start_autopilot_stage(
            task_id=job.task_id,
            stage="chatgpt",
            attempt=max(1, job.attempts.get("chatgpt", 0)),
            log_path=str(info.path),
        )
        job.desktop_running = True
        job.desktop_marker = info.marker
        job.desktop_rollout = str(info.path)
        job.desktop_thread_id = thread_id_from_rollout(info.path)
        job.desktop_workdir = info.cwd or job.desktop_workdir
        job.desktop_started_at = now
        job.desktop_last_progress_at = now
        job.desktop_recovery_attempts = 0
        job.desktop_recovery_last_at = ""
        job.desktop_recovery_error = ""
        job.log = str(info.path)
        self.desktop_terminal_seen.discard(job.task_id)
        self._stage(job, "chatgpt", "已接管该任务仍在运行的 ChatGPT 会话，不再重复新开")
        self.repo.add_event(
            "autopilot.chatgpt.adopted",
            "该任务已有 ChatGPT 会话仍在运行，已重新接管而不是新开会话",
            actor=ACTOR,
            task_id=job.task_id,
            details={"rollout": str(info.path), "marker": info.marker},
        )
        self._ensure_desktop_follower(job)
        return True

    async def _launch_chatgpt(self, job: Job, root: Path, missing: list[str]) -> None:
        if self._adopt_live_session(job):
            return
        used = job.attempts.get("chatgpt", 0)
        if used >= self.stage_attempts:
            self._finish(job, "failed", f"ChatGPT 桌面交付已尝试 {used} 次仍未通过")
            return
        allowed, wait_text = await self._session_slot(job)
        if not allowed:
            self._stage(job, "pending", wait_text)
            return
        job.attempts["chatgpt"] = used + 1
        prompt = self.prompt_templates.render(
            "chatgpt",
            root=root,
            skill_root=self.skill_root,
            missing=missing,
            previous=job.detail,
            history=self.prompt_history.notes(job.task_id),
        )
        stage_id = self.repo.start_autopilot_stage(
            task_id=job.task_id,
            stage="chatgpt",
            attempt=job.attempts["chatgpt"],
            log_path="",
        )
        launched = await asyncio.to_thread(
            self.desktop.launch,
            task_id=job.task_id,
            task_name=job.task_name,
            task_root=root,
            prompt=prompt,
            folder_id=self.settings.chatgpt_folder_id,
        )
        job.desktop_running = True
        job.desktop_marker = str(launched["marker"])
        job.desktop_prompt_file = str(launched["prompt_path"])
        job.desktop_workdir = str(launched["workdir"])
        job.desktop_deep_link = str(launched["deep_link"])
        job.desktop_started_at = str(launched["started_at"])
        job.desktop_last_progress_at = str(launched["started_at"])
        job.desktop_recovery_attempts = 0
        job.desktop_recovery_last_at = ""
        job.desktop_recovery_error = ""
        job.desktop_rollout = ""
        job.desktop_thread_id = ""
        job.desktop_stage_id = stage_id
        self.desktop_terminal_seen.discard(job.task_id)
        job.log = job.desktop_prompt_file
        self._stage(job, "chatgpt", "已打开 ChatGPT 桌面会话，等待执行")
        self.repo.add_event(
            "autopilot.chatgpt.started",
            "已在 ChatGPT 桌面端创建会话并提交任务",
            actor=ACTOR,
            task_id=job.task_id,
            details={
                "prompt": job.desktop_prompt_file,
                "opened_with": launched["opened_with"],
                "attempt": job.attempts["chatgpt"],
            },
        )
        self._ensure_desktop_follower(job)

    def _ensure_desktop_follower(self, job: Job) -> None:
        if not job.desktop_running or not job.desktop_marker or job.task_id in self.desktop_followers:
            return
        started = _parse_datetime(job.desktop_started_at)
        if started is None:
            started = datetime.now().astimezone()
        self.desktop_followers[job.task_id] = asyncio.create_task(
            self._follow_chatgpt_rollout(job.task_id, job.desktop_marker, started)
        )

    async def _follow_chatgpt_rollout(self, task_id: str, marker: str, started: datetime) -> None:
        offset = 0
        announced = False
        try:
            while True:
                job = self.jobs.get(task_id)
                if not job or not job.desktop_running:
                    return
                path = Path(job.desktop_rollout) if job.desktop_rollout else None
                if path is None:
                    await self.refresh_live_sessions()
                    found = self.rollouts.find(marker)
                    path = found.path if found is not None else None
                    if path is not None:
                        job.desktop_rollout = str(path)
                        job.desktop_thread_id = thread_id_from_rollout(path)
                        job.log = str(path)
                        self._changed()
                if path and path.is_file():
                    frames, offset = rollout_delta(path, offset)
                    if frames:
                        self.bus.publish(
                            "autopilot.log",
                            {"task_id": task_id, "stage": "chatgpt", "frames": frames},
                        )
                    if not announced:
                        announced = True
                        self.repo.add_event(
                            "autopilot.chatgpt.attached",
                            "已连接到 ChatGPT 桌面会话轨迹",
                            actor=ACTOR,
                            task_id=task_id,
                            details={"rollout": str(path), "thread_id": job.desktop_thread_id},
                        )
                if path is None:
                    now = datetime.now().astimezone()
                    await self._recover_missing_chatgpt_submission(job, started=started, now=now)
                    elapsed = (now - started).total_seconds()
                    if elapsed >= CHATGPT_LAUNCH_TIMEOUT_SECONDS:
                        tried = (
                            f"，已自动补按发送 {job.desktop_recovery_attempts} 次"
                            if job.desktop_recovery_attempts
                            else ""
                        )
                        detail = (
                            f"发出深链接后 {CHATGPT_LAUNCH_TIMEOUT_SECONDS} 秒仍未建立可跟踪会话{tried}"
                        )
                        await self._finish_desktop_stage(
                            job,
                            outcome="failed",
                            kind="agent_launch_lost",
                            detail=detail,
                        )
                        self._finish(job, "failed", detail)
                        return
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("ChatGPT rollout follower failed: %s", task_id)
        finally:
            self.desktop_followers.pop(task_id, None)

    async def _recover_missing_chatgpt_submission(
        self,
        job: Job,
        *,
        started: datetime,
        now: datetime,
    ) -> None:
        """Nudge ChatGPT after a deep link opens without creating a rollout."""
        attempt = job.desktop_recovery_attempts
        if attempt >= len(CHATGPT_SUBMIT_RECOVERY_AT_SECONDS):
            return
        elapsed = (now - started).total_seconds()
        if elapsed < CHATGPT_SUBMIT_RECOVERY_AT_SECONDS[attempt]:
            return
        result = await asyncio.to_thread(
            self.desktop.submit_pending_prompt,
            job.desktop_deep_link,
        )
        job.desktop_recovery_attempts = attempt + 1
        job.desktop_recovery_last_at = _now()
        job.desktop_recovery_error = "" if result.get("ok") else str(result.get("message") or "补按发送失败")
        done = job.desktop_recovery_attempts
        total = len(CHATGPT_SUBMIT_RECOVERY_AT_SECONDS)
        if result.get("ok"):
            detail = f"ChatGPT 已打开但未收到首条消息，已自动补按发送（{done}/{total}）"
            level = "info"
            action = "autopilot.chatgpt.submit_recovered"
        else:
            detail = f"ChatGPT 已打开但未收到首条消息，自动补发失败（{done}/{total}）：{job.desktop_recovery_error}"
            level = "warn"
            action = "autopilot.chatgpt.submit_recovery_failed"
        self._stage(job, "chatgpt", detail)
        self.repo.add_event(
            action,
            detail,
            level=level,
            actor=ACTOR,
            task_id=job.task_id,
            details={
                "attempt": done,
                "max_attempts": total,
                "ok": bool(result.get("ok")),
                "message": job.desktop_recovery_error,
                "opened_with": str(result.get("opened_with") or ""),
            },
        )
        self._changed()

    async def _finish_desktop_stage(
        self,
        job: Job,
        *,
        outcome: str,
        kind: str = "",
        detail: str = "",
    ) -> None:
        if job.desktop_stage_id:
            self.repo.finish_autopilot_stage(
                job.desktop_stage_id,
                outcome=outcome,
                failure_kind=kind,
                failure_detail=detail,
            )
        job.desktop_running = False
        job.desktop_stage_id = 0
        job.agent_pid = 0
        if self.dispatcher is not None and self.dispatcher.v3_enabled:
            self.dispatcher.release_session(job.task_id)
        self.desktop_terminal_seen.discard(job.task_id)
        if outcome == "ok":
            self.breaker.record_success()
        await self._stop_desktop_follower(job.task_id)
        self.repo.add_event(
            f"autopilot.chatgpt.{outcome}",
            "ChatGPT 桌面会话完成" if outcome == "ok" else f"ChatGPT 桌面会话失败：{detail[:300]}",
            level="info" if outcome == "ok" else "warn",
            actor=ACTOR,
            task_id=job.task_id,
            details={"rollout": job.desktop_rollout, "thread_id": job.desktop_thread_id},
        )
        if outcome == "failed" and self.dispatcher is not None and self.dispatcher.v3_enabled:
            await self.dispatcher.on_failure(
                job.task_id,
                Evidence(
                    stage="chatgpt",
                    trace_tail=detail,
                    context={"failure_kind": _dispatch_failure_kind(kind)},
                ),
                actor=ACTOR,
            )
        self._changed()

    async def _stop_desktop_follower(self, task_id: str) -> None:
        task = self.desktop_followers.pop(task_id, None)
        if task and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _export_chatgpt(self, job: Job, task: dict[str, Any], root: Path) -> None:
        with suppress(Exception):
            await self.reconciler.run()
        facts = evaluate_steps(
            root,
            duplicate_sessions=self.repo.session_counts(),
            require_review_ack=False,
        )
        if not all(step.ok for step in facts.steps[:10]):
            await self._launch_chatgpt(job, root, facts.missing)
            return
        if self._reject_duplicate(job, root):
            return
        self._stage(job, "exporting", "ChatGPT 已通过前 10 项，平台检查并导出")
        check = await self.feedback.check(job.task_id)
        if not check["ok"]:
            await self._launch_chatgpt(
                job,
                root,
                [str(item) for item in check["errors"]] or ["Feedback 检查未通过"],
            )
            return
        self.feedback.ack(job.task_id, acked=True, actor=ACTOR)
        result = await self.exports.export(task_ids=[job.task_id])
        if evaluate_steps(root, duplicate_sessions=self.repo.session_counts()).complete:
            await self._complete_or_audit(job, task, root)
            return
        reasons = [str(reason) for item in result.get("skipped") or [] for reason in item.get("reason") or []]
        self._finish(job, "failed", "导出被拒绝：" + ("；".join(reasons[:3]) or "原因未知"))

    # ---- agent runner ----------------------------------------------

    async def _launch(self, job: Job, stage: str, root: Path, missing: list[str]) -> None:
        used = job.attempts.get(stage, 0)
        if used >= self.stage_attempts:
            reason = job.detail or "；".join(missing[:2])
            self._finish(job, "failed", f"{STAGE_LABELS[stage]} 已尝试 {used} 次仍未通过：{reason}")
            return
        if self.dispatcher is not None and self.dispatcher.v3_enabled:
            decision = self.dispatcher.request_agent(job.task_id, stage=stage)
            if not decision.allowed:
                self._stage(job, stage, decision.text or "等待 Agent 空位")
                return
        if sum(1 for run in self.agents.values() if run.stage == stage) >= self.parallel[stage]:
            self._stage(job, stage, "等待 Agent 空位")
            return
        job.attempts[stage] = used + 1
        log_dir = self.log_root / job.task_id
        log_dir.mkdir(parents=True, exist_ok=True)
        log = log_dir / f"{stage}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.jsonl"
        prompt = self.prompt_templates.render(
            stage,
            root=root,
            skill_root=self.skill_root,
            missing=missing,
            previous=job.detail,
            history=self.prompt_history.notes(job.task_id) if stage == "authoring" else None,
        )
        stage_id = self.repo.start_autopilot_stage(
            task_id=job.task_id,
            stage=stage,
            attempt=job.attempts[stage],
            log_path=str(log),
        )
        handle = log.open("wb")
        try:
            process = await asyncio.create_subprocess_exec(
                *self.agent_command,
                "-C",
                str(root),
                prompt,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=handle,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
        finally:
            handle.close()
        job.log = str(log)
        job.agent_pid = process.pid
        waiter = asyncio.create_task(self._wait_agent(job, stage, process, log))
        done = asyncio.Event()
        streamer = asyncio.create_task(
            AgentLogStreamer(path=log, task_id=job.task_id, stage=stage, bus=self.bus).run(done)
        )
        self.agents[job.task_id] = AgentRun(stage, process, waiter, log, stage_id, streamer, done)
        self._stage(job, stage, f"Agent 运行中（第 {job.attempts[stage]} 次）")
        self.repo.add_event(
            f"autopilot.{stage}.started",
            f"已拉起 Agent：{STAGE_LABELS[stage]}",
            actor=ACTOR,
            task_id=job.task_id,
            details={"log": str(log), "attempt": job.attempts[stage]},
        )

    async def _wait_agent(self, job: Job, stage: str, process: asyncio.subprocess.Process, log: Path) -> None:
        timed_out = False
        try:
            await asyncio.wait_for(process.wait(), timeout=self.agent_timeout_seconds)
        except TimeoutError:
            timed_out = True
            _signal_group(process, signal.SIGKILL)
            await process.wait()
        except asyncio.CancelledError:
            return
        run = self.agents.pop(job.task_id, None)
        job.agent_pid = 0
        if self.dispatcher is not None and self.dispatcher.v3_enabled:
            self.dispatcher.release_agent(job.task_id, stage=stage)
        if run:
            run.done.set()
            if run.streamer:
                await asyncio.gather(run.streamer, return_exceptions=True)
        verdict = "超过时限被终止" if timed_out else _verdict(log)
        code = process.returncode
        succeeded = code == 0 and verdict.startswith("OK")
        failure_kind = "" if succeeded else self._failure_kind(stage, verdict, code)
        if run and run.stage_id:
            self.repo.finish_autopilot_stage(
                run.stage_id,
                outcome="ok" if succeeded else ("stopped" if code is not None and code < 0 else "failed"),
                failure_kind=failure_kind,
                failure_detail=verdict,
                usage_data=usage(log),
            )
        if succeeded:
            self.breaker.record_success()
            if stage == "delivering" and job.redo_feedback:
                job.redo_feedback = False
        else:
            tripped = False
            if self.dispatcher is not None and self.dispatcher.v3_enabled:
                verdict = (
                    f"AUTOPILOT: {verdict}"
                    if verdict.startswith(("OK", "FAILED", "BLOCKED"))
                    else f"AUTOPILOT: FAILED {verdict}"
                )
                await self.dispatcher.on_failure(
                    job.task_id,
                    Evidence(
                        stage=stage,
                        exit_code=code,
                        agent_verdict=verdict,
                        trace_tail="\n".join(
                            str(line.get("text") or "") for line in read_log(log, 80)
                        )
                        if log.is_file()
                        else "",
                    ),
                    actor=ACTOR,
                )
            else:
                tripped = self.breaker.record_failure(failure_kind, verdict or f"exit {code}")
            job.failure_kind = failure_kind
            if tripped:
                self.paused = True
                self.bus.publish(
                    "toast",
                    {"level": "error", "message": f"{job.task_name} 自动交付失败，流水线已暂停"},
                )
        with suppress(Exception):
            await self.reconciler.run()
        if job.stage == stage:
            job.detail = "Agent 已结束，核对磁盘事实" if succeeded else f"Agent 未完成（exit {code}）：{verdict}"
            job.updated_at = _now()
        self.repo.add_event(
            f"autopilot.{stage}.finished",
            f"Agent 结束：{STAGE_LABELS[stage]}（{verdict or f'exit {code}'}）",
            level="info" if succeeded else "warning",
            actor=ACTOR,
            task_id=job.task_id,
            details={"exit_code": code, "verdict": verdict, "log": str(log)},
        )
        if not succeeded:
            self.repo.add_event(
                "autopilot.failure_classified",
                f"{STAGE_LABELS[stage]}失败：{failure_kind}",
                level="warn",
                actor=ACTOR,
                task_id=job.task_id,
                details={"failure_kind": failure_kind, "detail": verdict},
            )
        self._changed()

    async def _kill_agent(self, task_id: str) -> None:
        run = self.agents.pop(task_id, None)
        if not run:
            return
        if self.dispatcher is not None and self.dispatcher.v3_enabled:
            self.dispatcher.release_agent(task_id, stage=run.stage)
        run.waiter.cancel()
        run.done.set()
        if run.streamer:
            run.streamer.cancel()
        _signal_group(run.process, signal.SIGTERM)
        try:
            await asyncio.wait_for(run.process.wait(), timeout=5)
        except TimeoutError:
            _signal_group(run.process, signal.SIGKILL)
            await run.process.wait()
        stopped_job = self.jobs.get(task_id)
        if stopped_job:
            stopped_job.agent_pid = 0

    def _failure_kind(self, stage: str, detail: str, code: int | None) -> str:
        lowered = detail.lower()
        if "BLOCKED" in detail[:40].upper():
            return "agent_blocked"
        if "超过时限" in detail:
            return "agent_timeout"
        if "雷同" in detail:
            return "prompt_duplicate"
        if "401" in detail or "authenticat" in lowered or "鉴权" in detail:
            return "auth"
        if "门禁" in detail or "errors" in lowered:
            return "gate_rejected"
        if "录屏" in detail or "record" in lowered:
            return "recording"
        if "导出" in detail or "export" in lowered:
            return "export"
        if "docker" in lowered or "磁盘" in detail or "找不到 Agent" in detail:
            return "environment"
        if stage == "running":
            return "run_incomplete"
        return "agent_reported" if code != -signal.SIGKILL else "agent_timeout"

    # ---- bookkeeping -----------------------------------------------

    def _stage(self, job: Job, stage: str, detail: str) -> None:
        if job.stage == stage and job.detail == detail:
            return
        job.stage, job.detail, job.updated_at = stage, detail, _now()
        job.stage_started_at = job.updated_at
        self._changed()

    def _finish(self, job: Job, stage: str, detail: str) -> None:
        job.stage, job.detail = stage, detail
        job.updated_at = job.finished_at = _now()
        if (
            self.dispatcher is not None
            and self.dispatcher.v3_enabled
            and stage in {"done", "awaiting_audit", "stopped"}
        ):
            self.dispatcher.on_job_finished(
                job.task_id,
                outcome=stage,
                detail=detail,
            )
        if stage == "failed":
            newly_classified = not job.failure_kind
            job.failure_kind = job.failure_kind or self._failure_kind("", detail, None)
            if newly_classified and not (self.dispatcher is not None and self.dispatcher.v3_enabled):
                self.breaker.record_failure(job.failure_kind, detail)
            if self.ledger:
                self.ledger.mark_failure(job.task_id, job.failure_kind)
            if self.notifier:
                self.notifier.notify(
                    title="solo-auto 自动交付失败",
                    status=job.failure_kind or "失败",
                    task_name=job.task_name,
                )
        elif stage == "done":
            self.breaker.record_success()
            job.failure_kind = ""
            if self.notifier:
                self.notifier.notify(title="solo-auto 自动交付完成", status="完成", task_name=job.task_name)
        self.repo.add_event(
            f"autopilot.{stage}",
            f"自动交付{STAGE_LABELS[stage]}：{detail}",
            level="error" if stage == "failed" else "info",
            actor=ACTOR,
            task_id=job.task_id,
        )
        self._changed()

    def _job_view(self, job: Job) -> dict[str, Any]:
        try:
            elapsed = max(
                0,
                (datetime.fromisoformat(_now()) - datetime.fromisoformat(job.stage_started_at)).total_seconds(),
            )
        except ValueError:
            elapsed = 0
        return {
            **asdict(job),
            "stage_label": STAGE_LABELS.get(job.stage, job.stage),
            "active": job.active,
            "agent_running": job.task_id in self.agents or job.desktop_running,
            "elapsed_seconds": round(elapsed, 1),
            "stages": self.repo.autopilot_stages(job.task_id),
            "retry_stages": self._retry_stages(job),
            "retry_options": [
                {
                    "stage": stage,
                    "label": STAGE_LABELS[stage],
                    "available": not bool(self._retry_reason(job, stage)),
                    "reason": self._retry_reason(job, stage),
                }
                for stage in ("authoring", "running", "delivering", "exporting")
            ],
            "audit": self.repo.audit(job.task_id),
        }

    def _retry_stages(self, job: Job) -> list[str]:
        if self.settings.agent_mode == "chatgpt":
            return ["chatgpt"] if not self._retry_reason(job, "chatgpt") else []
        return [
            stage
            for stage in ("authoring", "running", "delivering", "exporting")
            if not self._retry_reason(job, stage)
        ]

    def _retry_reason(self, job: Job, stage: str) -> str:
        task = self.repo.get_task(job.task_id)
        if not task:
            return "任务不存在"
        envelope = load_state(Path(task["task_root"]))
        if envelope.state is None:
            return envelope.error or "任务状态不可读"
        if stage == "chatgpt":
            return ""
        state = envelope.state
        if stage == "authoring" and state.session is not None:
            return "已有会话，不能重写题面"
        if stage == "running":
            if state.session is not None:
                return "已有被接受的会话，不能重新运行"
            facts = evaluate_steps(Path(task["task_root"]), require_review_ack=False)
            if not all(step.ok for step in facts.steps[:3]):
                return "题面前三项尚未通过"
        if stage in {"delivering", "exporting"} and state.session is None:
            return "尚无被接受的会话"
        if stage not in {"authoring", "running", "delivering", "exporting"}:
            return "未知阶段"
        return ""

    def _changed(self) -> None:
        payload = {
            "paused": self.paused,
            "jobs": [asdict(job) for job in self.jobs.values()],
        }
        tmp = self.state_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self.state_file)
        self.bus.publish("autopilot.updated", {"active": sum(1 for job in self.jobs.values() if job.active)})

    def _load(self) -> None:
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        self.paused = bool(data.get("paused"))
        known = set(Job.__dataclass_fields__)
        for item in data.get("jobs") or []:
            if isinstance(item, dict) and item.get("task_id"):
                self.jobs[str(item["task_id"])] = Job(**{key: value for key, value in item.items() if key in known})
        for job in self.jobs.values():
            if job.agent_pid and job.stage in AGENT_STAGES:
                with suppress(ProcessLookupError, PermissionError):
                    os.killpg(job.agent_pid, signal.SIGKILL)
                job.agent_pid = 0
                job.stage = "pending"
                job.detail = "后端重启：已中止旧 Agent 进程，等待按磁盘事实续跑"
                job.stage_started_at = _now()
                job.updated_at = _now()
            if job.desktop_running:
                task = self.repo.get_task(job.task_id)
                if task:
                    meaningful = _meaningful_progress_at(Path(str(task["task_root"])))
                    fallback = _parse_datetime(job.desktop_started_at) or datetime.now().astimezone()
                    job.desktop_last_progress_at = (meaningful or fallback).replace(microsecond=0).isoformat()


def _signal_group(process: asyncio.subprocess.Process, sig: int) -> None:
    with suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, sig)


def _dispatch_failure_kind(kind: str) -> str:
    return {
        "agent_launch_lost": "agent.launch_lost",
        "usage_limited": "agent.usage_limited",
        "agent_timeout": "agent.timeout",
        "agent_blocked": "agent.blocked",
        "agent_reported": "agent.reported",
        "agent_no_progress": "agent.no_progress",
        "agent_aborted": "agent.aborted",
        "recording_failed": "recording.failed",
        "recording_permission": "recording.permission",
        "recording_privacy": "recording.privacy",
        "export_failed": "export.failed",
        "gate_rejected": "gate.rejected",
        "auth": "gateway.auth",
        "environment": "docker.unavailable",
        "run_timeout": "run.timeout",
        "run_incomplete": "run.incomplete",
    }.get(kind, kind or "agent.reported")


def _parse_datetime(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else parsed.astimezone()


def _events(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    data = {"type": "raw", "text": line}
                events.append(data if isinstance(data, dict) else {"type": "raw", "text": line})
    except OSError:
        pass
    return events


def _verdict(path: Path) -> str:
    """The agent's closing ``AUTOPILOT: OK|FAILED`` line; empty when it never wrote one."""
    for event in reversed(_events(path)):
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") == "agent_message":
            for line in reversed(str(item.get("text") or "").splitlines()):
                if VERDICT in line:
                    return line.split(VERDICT, 1)[1].strip()[:300]
    return ""


def _render_log(path: Path, limit: int) -> list[dict[str, str]]:
    lines: list[dict[str, str]] = []
    for event in _events(path):
        kind = str(event.get("type") or "")
        item = event.get("item")
        if isinstance(item, dict):
            if kind != "item.completed":
                continue
            item_type = str(item.get("type") or "")
            if item_type == "command_execution":
                output = str(item.get("aggregated_output") or "").strip()
                text = f"$ {item.get('command')}  → exit {item.get('exit_code')}"
                if output:
                    text += "\n" + output[-600:]
                lines.append({"kind": "command", "text": text})
            elif item_type in {"agent_message", "reasoning"}:
                lines.append(
                    {
                        "kind": "message" if item_type == "agent_message" else "thinking",
                        "text": str(item.get("text") or ""),
                    }
                )
            elif item_type == "error":
                lines.append({"kind": "error", "text": str(item.get("message") or "")})
            else:
                lines.append({"kind": "other", "text": item_type})
        elif kind in {"raw", "error"}:
            lines.append({"kind": kind, "text": str(event.get("text") or event.get("message") or "")})
    return lines[-limit:]


def build_prompt(stage: str, *, root: Path, skill_root: Path, missing: list[str], previous: str = "") -> str:
    script = skill_root / "scripts" / "solo2611.py"
    head = f"""你在无人值守模式下工作：全程不要提问，不会有人回复你，遇到选择自己按技能规则决定。
使用 solo-2611 技能。先完整阅读 {skill_root / "SKILL.md"}，以及它在对应环节要求阅读的 references。
任务目录 ROOT={root}
脚本 S={script}
"""
    if stage == "authoring":
        body = """本次只做「出题」阶段：
1. 读 ROOT/workspace 里的真实源码，按技能的出题规则设计一道「困难」或「地狱」题。
2. 执行 python3 $S prompt --task-root ROOT --init，填好题面草稿和两份审核 JSON，再执行 prompt 安装，直到门禁通过。
3. 执行 python3 $S repo --task-root ROOT，把初始快照推到远端。
4. 执行 python3 $S status --task-root ROOT，确认前三项通过。
边界：不执行 run、interactive、collect，单轮会话由平台排队执行。门禁不过就改题面重来。"""
    else:
        body = """单轮会话已经结束。本次只做「复核、录屏、Feedback」阶段：
1. 读原始轨迹、轨迹时间线和初始快照到产物快照的 diff，并在本机真实运行产物做复核（技能工作流第 6 步）。
2. 录屏：record plan 生成计划，填写后 record check-plan，再用 python3 $S record auto --task-root ROOT 一次录完。
   Web 题按 references/recording.md 的独立 Chrome 流程录。产物跑不起来也照录，画面保留真实报错。
3. 执行 feedback --init，按 references/feedback-guide.md 起草 Feedback（文件位置以技能说明为准），
   再执行 feedback --check，改到没有错误为止。
4. 执行 python3 $S status --task-root ROOT，确认第 9 项通过、Feedback 机器检查通过。
边界：不执行 run、reset、export、cleanup。复核时产生的临时文件用完清掉，不提交、不推送。
这次交付没有人工复核，你写下的分数和事实就是最终内容。每个结论都要能回查到轨迹或你亲手跑出的结果，拿不准的不写。"""
    rules = """通用边界：不修改技能目录、校验器和平台文件；不使用任何强制通过、允许缺项之类绕过门禁的参数；
不伪造任何证据文件。门禁确实过不去时如实报告失败。
全部结束后，最后一行只输出下面三种之一：
AUTOPILOT: OK
AUTOPILOT: FAILED <一句话原因，可在后续尝试中恢复>
AUTOPILOT: BLOCKED <一句话原因，按规则停止并需要人工处理>"""
    notes = ""
    if missing:
        notes += "\n平台当前看到的缺项：\n" + "\n".join(f"- {item}" for item in missing[:6])
    if previous and "未完成" in previous:
        notes += f"\n上一次尝试的结果：{previous}"
    return f"{head}\n{body}\n\n{rules}{notes}\n"
