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
from solo_auto.pipeline.export_service import ExportService
from solo_auto.pipeline.feedback_service import FeedbackService
from solo_auto.pipeline.reconcile import Reconciler
from solo_auto.pipeline.scheduler import RunScheduler
from solo_auto.pipeline.steps import evaluate_steps
from solo_auto.settings import Paths
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

    @property
    def active(self) -> bool:
        return self.stage in ACTIVE_STAGES


@dataclass(slots=True)
class AgentRun:
    stage: str
    process: asyncio.subprocess.Process
    waiter: asyncio.Task[None]
    log: Path


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
    ) -> None:
        self.paths = paths
        self.skill_root = skill_root
        self.repo = repo
        self.reconciler = reconciler
        self.bus = bus
        self.scheduler = scheduler
        self.feedback = feedback
        self.exports = exports
        self.agent_command = default_agent_command()
        self.agent_timeout_seconds = 60 * 60
        self.stage_attempts = 2
        self.run_attempts = 2
        self.parallel = {"authoring": 2, "delivering": 1}  # 录屏独占主机窗口，交付阶段一次只跑一个
        self.tick_seconds = 3.0
        self.state_file = paths.home / "autopilot.json"
        self.log_root = paths.logs / "autopilot"
        self.jobs: dict[str, Job] = {}
        self.agents: dict[str, AgentRun] = {}
        self._loop_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._load()

    # ---- lifecycle -------------------------------------------------

    async def start(self) -> None:
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
            "agent_command": self.agent_command[0],
            "agent_available": bool(shutil.which(self.agent_command[0])),
            "agents_running": len(self.agents),
            "active": sum(1 for job in jobs if job.active),
            "jobs": [self._job_view(job) for job in jobs],
        }

    def job(self, task_id: str) -> dict[str, Any] | None:
        job = self.jobs.get(task_id)
        return self._job_view(job) if job else None

    async def enroll(self, task_ids: list[str]) -> dict[str, Any]:
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

    async def cancel(self, task_ids: list[str] | None = None) -> dict[str, Any]:
        targets = task_ids or [job.task_id for job in self.jobs.values() if job.active]
        stopped: list[str] = []
        for task_id in targets:
            job = self.jobs.get(task_id)
            if not job or not job.active:
                continue
            await self._kill_agent(task_id)
            self._finish(job, "stopped", "已手动停止；已入队或正在运行的容器不受影响")
            stopped.append(task_id)
        self._changed()
        return {"stopped": stopped, **self.snapshot()}

    def forget(self, task_id: str) -> None:
        """Drop the record of a task that has been removed from the platform."""
        if self.jobs.pop(task_id, None) is not None:
            self._changed()

    def log_tail(self, task_id: str, limit: int = 200) -> dict[str, Any]:
        job = self.jobs.get(task_id)
        if not job:
            raise ApiError(404, "AUTOPILOT_JOB_NOT_FOUND", "该任务没有自动交付记录")
        path = Path(job.log) if job.log else None
        lines = _render_log(path, limit) if path and path.is_file() else []
        return {"task_id": task_id, "stage": job.stage, "file": job.log, "lines": lines}

    # ---- state machine ---------------------------------------------

    async def tick(self) -> None:
        async with self._lock:
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
        if final.complete:
            self._finish(job, "done", "十一项交付全部通过")
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
        if not final.steps[9].ok:
            check = await self.feedback.check(job.task_id)
            if not check["ok"]:
                reasons = [str(item) for item in check["errors"]] or ["Feedback 检查未通过"]
                await self._launch(job, "delivering", root, reasons)
                return
            self.feedback.ack(job.task_id, acked=True, actor=ACTOR)
        result = await self.exports.export(task_ids=[job.task_id])
        if evaluate_steps(root, duplicate_sessions=self.repo.session_counts()).complete:
            self._finish(job, "done", f"已导出：{result.get('file') or '交付表.xlsx'}")
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
        if sum(1 for run in self.agents.values() if run.stage == stage) >= self.parallel[stage]:
            self._stage(job, stage, "等待 Agent 空位")
            return
        job.attempts[stage] = used + 1
        log_dir = self.log_root / job.task_id
        log_dir.mkdir(parents=True, exist_ok=True)
        log = log_dir / f"{stage}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.jsonl"
        prompt = build_prompt(stage, root=root, skill_root=self.skill_root, missing=missing, previous=job.detail)
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
        waiter = asyncio.create_task(self._wait_agent(job, stage, process, log))
        self.agents[job.task_id] = AgentRun(stage, process, waiter, log)
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
        self.agents.pop(job.task_id, None)
        verdict = "超过时限被终止" if timed_out else _verdict(log)
        code = process.returncode
        succeeded = code == 0 and verdict.startswith("OK")
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
        self._changed()

    async def _kill_agent(self, task_id: str) -> None:
        run = self.agents.pop(task_id, None)
        if not run:
            return
        run.waiter.cancel()
        _signal_group(run.process, signal.SIGTERM)
        try:
            await asyncio.wait_for(run.process.wait(), timeout=5)
        except TimeoutError:
            _signal_group(run.process, signal.SIGKILL)
            await run.process.wait()

    # ---- bookkeeping -----------------------------------------------

    def _stage(self, job: Job, stage: str, detail: str) -> None:
        if job.stage == stage and job.detail == detail:
            return
        job.stage, job.detail, job.updated_at = stage, detail, _now()
        self._changed()

    def _finish(self, job: Job, stage: str, detail: str) -> None:
        job.stage, job.detail = stage, detail
        job.updated_at = job.finished_at = _now()
        self.repo.add_event(
            f"autopilot.{stage}",
            f"自动交付{STAGE_LABELS[stage]}：{detail}",
            level="error" if stage == "failed" else "info",
            actor=ACTOR,
            task_id=job.task_id,
        )
        self._changed()

    def _job_view(self, job: Job) -> dict[str, Any]:
        return {
            **asdict(job),
            "stage_label": STAGE_LABELS.get(job.stage, job.stage),
            "active": job.active,
            "agent_running": job.task_id in self.agents,
        }

    def _changed(self) -> None:
        payload = {"jobs": [asdict(job) for job in self.jobs.values()]}
        tmp = self.state_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self.state_file)
        self.bus.publish("autopilot.updated", {"active": sum(1 for job in self.jobs.values() if job.active)})

    def _load(self) -> None:
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        known = set(Job.__dataclass_fields__)
        for item in data.get("jobs") or []:
            if isinstance(item, dict) and item.get("task_id"):
                self.jobs[str(item["task_id"])] = Job(**{key: value for key, value in item.items() if key in known})


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
