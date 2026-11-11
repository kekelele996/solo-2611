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
import shutil
import signal
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from solo_auto.errors import ApiError
from solo_auto.events.bus import EventBus
from solo_auto.gateway.skill import SkillGateway
from solo_auto.pipeline.agent_stream import AgentLogStreamer, read_log, usage
from solo_auto.pipeline.breaker import Breaker
from solo_auto.pipeline.export_service import ExportService
from solo_auto.pipeline.feedback_service import FeedbackService
from solo_auto.pipeline.notify import Notifier
from solo_auto.pipeline.prompt_templates import PromptTemplates
from solo_auto.pipeline.reconcile import Reconciler
from solo_auto.pipeline.scheduler import RunScheduler
from solo_auto.pipeline.steps import evaluate_steps
from solo_auto.settings import AutopilotSettings, Paths
from solo_auto.store.repo import Repository
from solo_auto.store.state import load_state

LOGGER = logging.getLogger(__name__)

ACTOR = "autopilot"
ACTIVE_STAGES = ("pending", "authoring", "running", "delivering", "exporting")
AGENT_STAGES = ("authoring", "delivering")
STAGE_LABELS = {
    "pending": "等待调度",
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


def _now() -> str:
    return datetime.now().astimezone().replace(microsecond=0).isoformat()


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
    agent_pid: int = 0

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
        self.settings = settings
        self.breaker = Breaker(paths.home / "breaker.json", bus, settings)
        self.prompt_templates = PromptTemplates(paths)
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

    # ---- lifecycle -------------------------------------------------

    async def start(self) -> None:
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
            "paused": self.paused or self.breaker.tripped,
            "paused_by_breaker": self.breaker.tripped,
            "breaker": self.breaker.state,
            "agent_command": self.agent_command[0],
            "agent_available": bool(shutil.which(self.agent_command[0])),
            "agents_running": len(self.agents),
            "active": sum(1 for job in jobs if job.active),
            "parallel_authoring": self.settings.parallel_authoring,
            "parallel_delivering": self.settings.parallel_delivering,
            "stage_timeout_seconds": self.settings.stage_timeout_seconds,
            "jobs": [self._job_view(job) for job in jobs],
        }

    def job(self, task_id: str) -> dict[str, Any] | None:
        job = self.jobs.get(task_id)
        return self._job_view(job) if job else None

    async def enroll(self, task_ids: list[str]) -> dict[str, Any]:
        if not self.settings.enabled:
            raise ApiError(409, "AUTOPILOT_DISABLED", "自动交付总开关已关闭")
        if self.breaker.tripped:
            raise ApiError(409, "AUTOPILOT_BREAKER_OPEN", "自动交付已熔断，请先恢复")
        if not shutil.which(self.agent_command[0]):
            raise ApiError(
                409,
                "AGENT_NOT_FOUND",
                f"找不到 Agent 命令：{self.agent_command[0]}。请安装 Codex CLI 或设置 SOLO_AUTO_AGENT_BIN",
            )
        added: list[str] = []
        for task_id in dict.fromkeys(task_ids):
            task = self.repo.get_task(task_id)
            if not task:
                raise ApiError(404, "TASK_NOT_FOUND", f"任务不存在：{task_id}")
            current = self.jobs.get(task_id)
            if current and current.active:
                continue
            self.jobs[task_id] = Job(task_id=task_id, task_name=str(task["task_name"]))
            added.append(task_id)
            self.repo.add_event(
                "autopilot.enrolled",
                f"{task['task_name']} 进入自动交付（无人工复核）",
                actor="user",
                task_id=task_id,
            )
        self._changed()
        return {"added": added, **self.snapshot()}

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
            cleanup_detail = ""
            if should_kill:
                task = self.repo.get_task(task_id)
                if task:
                    if task_id in self.scheduler.active or self.scheduler.queue_position(task_id) is not None:
                        with suppress(ApiError):
                            await self.scheduler.stop_task(task_id, force=True)
                    cleanup = await self.gateway.call(
                        ["cleanup", "--task-root", str(task["task_root"]), "--force"],
                        timeout=120,
                        task_id=task_id,
                    )
                    cleanup_detail = (
                        "；已中断容器"
                        if cleanup.code == 0
                        else f"；容器清理失败：{cleanup.err.strip() or cleanup.code}"
                    )
            self._finish(job, "stopped", "已手动停止" + cleanup_detail)
            stopped.append(task_id)
        self._changed()
        return {"stopped": stopped, **self.snapshot()}

    def pause(self) -> dict[str, Any]:
        self.paused = True
        self._changed()
        return self.snapshot()

    def resume(self) -> dict[str, Any]:
        self.paused = False
        self.breaker.reset()
        self._changed()
        return self.snapshot()

    def mark_done(self, task_id: str, detail: str = "自动交付完成") -> None:
        job = self.jobs.get(task_id)
        if job:
            self._finish(job, "done", detail)

    def mark_failed(self, task_id: str, detail: str) -> None:
        job = self.jobs.get(task_id)
        if job:
            self._finish(job, "failed", detail)

    async def retry(self, task_id: str, *, from_stage: str = "") -> dict[str, Any]:
        job = self.jobs.get(task_id)
        if not job:
            raise ApiError(404, "AUTOPILOT_JOB_NOT_FOUND", "该任务没有自动交付记录")
        if job.active and job.stage != "failed":
            raise ApiError(409, "AUTOPILOT_ACTIVE", "自动交付仍在运行，先停止再重试")
        stages = self._retry_stages(job)
        if from_stage and from_stage not in stages:
            raise ApiError(409, "AUTOPILOT_STAGE_UNAVAILABLE", self._retry_reason(job, from_stage))
        target = from_stage or (stages[0] if stages else "")
        if not target:
            raise ApiError(409, "AUTOPILOT_NO_RETRY_STAGE", "磁盘事实不允许从任何阶段续跑")
        remove = False
        order = ["authoring", "running", "delivering", "exporting"]
        for stage in order:
            if stage == target:
                remove = True
            if remove:
                job.attempts.pop(stage, None)
        job.stage = "pending"
        job.detail = f"从「{STAGE_LABELS.get(target, target)}」续跑"
        job.failure_kind = ""
        job.finished_at = ""
        job.stage_started_at = _now()
        self._changed()
        return self._job_view(job)

    def stages(self, task_id: str) -> list[dict[str, Any]]:
        if task_id not in self.jobs:
            raise ApiError(404, "AUTOPILOT_JOB_NOT_FOUND", "该任务没有自动交付记录")
        return self.repo.autopilot_stages(task_id)

    def forget(self, task_id: str) -> None:
        """Drop the record of a task that has been removed from the platform."""
        if self.jobs.pop(task_id, None) is not None:
            self._changed()

    def log_tail(self, task_id: str, limit: int = 200, *, stage: str = "") -> dict[str, Any]:
        job = self.jobs.get(task_id)
        if not job:
            raise ApiError(404, "AUTOPILOT_JOB_NOT_FOUND", "该任务没有自动交付记录")
        path: Path | None = None
        if stage:
            row = next(
                (item for item in self.repo.autopilot_stages(task_id) if item.get("stage") == stage),
                None,
            )
            if row and row.get("log_path"):
                path = Path(str(row["log_path"]))
        if path is None and job.log:
            path = Path(job.log)
        lines = read_log(path, limit) if path and path.is_file() else []
        return {"task_id": task_id, "stage": stage or job.stage, "file": str(path or ""), "lines": lines}

    # ---- state machine ---------------------------------------------

    async def tick(self) -> None:
        async with self._lock:
            if not self.settings.enabled or self.paused or self.breaker.tripped:
                return
            for job in list(self.jobs.values()):
                if not job.active:
                    continue
                try:
                    await self._advance(job)
                except ApiError as error:
                    self._finish(job, "failed", f"{error.code}: {error.message}")
                except Exception as error:
                    LOGGER.exception("autopilot job failed: %s", job.task_id)
                    self._finish(job, "failed", f"{type(error).__name__}: {error}")

    async def _advance(self, job: Job) -> None:
        if job.task_id in self.agents:
            return
        task = self.repo.get_task(job.task_id)
        if not task:
            self._finish(job, "failed", "任务已不存在")
            return
        root = Path(task["task_root"])
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
            if str(task.get("job_status")) == "blocked":
                self._finish(job, "failed", "上次运行因鉴权失败被阻断，请检查网关 Key 后重试")
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

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    # ---- agent runner ----------------------------------------------

    async def _launch(self, job: Job, stage: str, root: Path, missing: list[str]) -> None:
        used = job.attempts.get(stage, 0)
        if used >= self.stage_attempts:
            reason = job.detail or "；".join(missing[:2])
            self._finish(job, "failed", f"{STAGE_LABELS[stage]} 已尝试 {used} 次仍未通过：{reason}")
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
        if "超过时限" in detail:
            return "agent_timeout"
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
        if stage == "failed":
            newly_classified = not job.failure_kind
            job.failure_kind = job.failure_kind or self._failure_kind("", detail, None)
            if newly_classified:
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
            "agent_running": job.task_id in self.agents,
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


def _signal_group(process: asyncio.subprocess.Process, sig: int) -> None:
    with suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, sig)


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
全部结束后，最后一行只输出下面两种之一：
AUTOPILOT: OK
AUTOPILOT: FAILED <一句话原因>"""
    notes = ""
    if missing:
        notes += "\n平台当前看到的缺项：\n" + "\n".join(f"- {item}" for item in missing[:6])
    if previous and "未完成" in previous:
        notes += f"\n上一次尝试的结果：{previous}"
    return f"{head}\n{body}\n\n{rules}{notes}\n"
