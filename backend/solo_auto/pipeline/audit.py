"""Deterministic sampling and strictly read-only human audit decisions."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from solo_auto.errors import ApiError
from solo_auto.events.bus import EventBus
from solo_auto.evidence.diff import diff_summary
from solo_auto.evidence.trace import timeline
from solo_auto.pipeline.breaker import Breaker
from solo_auto.pipeline.steps import evaluate_steps
from solo_auto.settings import AuditSettings, Paths
from solo_auto.store.repo import Repository
from solo_auto.store.state import load_state

if TYPE_CHECKING:
    from solo_auto.pipeline.autopilot import Autopilot
    from solo_auto.pipeline.feedback_service import FeedbackService
    from solo_auto.pipeline.prompt_service import PromptService
    from solo_auto.pipeline.recording_service import RecordingService


class AuditService:
    def __init__(
        self,
        *,
        paths: Paths,
        repo: Repository,
        bus: EventBus,
        settings: AuditSettings,
        breaker: Breaker,
        prompts: PromptService,
        feedback: FeedbackService,
        recording: RecordingService,
    ) -> None:
        self.paths = paths
        self.repo = repo
        self.bus = bus
        self.settings = settings
        self.breaker = breaker
        self.prompts = prompts
        self.feedback_service = feedback
        self.recording = recording
        self.autopilot: Autopilot | None = None

    def attach_autopilot(self, autopilot: Autopilot) -> None:
        self.autopilot = autopilot

    def assess(self, task_id: str, task_type: str = "") -> dict[str, Any]:
        existing = self.repo.audit(task_id)
        if existing:
            return existing
        if self.settings.mode == "all":
            sampled = 1
        elif self.settings.mode == "off":
            sampled = 0
        else:
            salt = task_type if self.settings.stratify_by == "task_type" else ""
            value = int(hashlib.sha256(f"{task_id}:{salt}".encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
            sampled = int(value < self.settings.sample_rate)
        record = {
            "task_id": task_id,
            "sampled": sampled,
            "verdict": None,
            "reason": "",
            "disposition": "",
            "decided_at": "",
        }
        self.repo.upsert_audit(task_id, record)
        self._write(task_id, {**record, "sampledAt": _now()})
        self.bus.publish("audit.updated", {"task_id": task_id, "sampled": bool(sampled)})
        return record

    def list(self, verdict: str | None = "pending") -> list[dict[str, Any]]:
        rows = self.repo.audits()
        if verdict == "pending":
            rows = [row for row in rows if row["sampled"] and not row["verdict"]]
        elif verdict:
            rows = [row for row in rows if row["verdict"] == verdict]
        return rows

    async def detail(self, task_id: str) -> dict[str, Any]:
        task = self.repo.get_task(task_id)
        if not task:
            raise ApiError(404, "TASK_NOT_FOUND", f"任务不存在：{task_id}")
        audit = self.repo.audit(task_id) or self.assess(task_id, str(task.get("task_type") or ""))
        root = Path(task["task_root"])
        evaluation = evaluate_steps(root, duplicate_sessions=self.repo.session_counts())
        envelope = load_state(root)
        state = envelope.state
        trace_path = Path(state.session.trace) if state and state.session and state.session.trace else None
        trace = timeline(trace_path) if trace_path and trace_path.is_file() else {"steps": [], "counts": {"total": 0, "failed": 0}}
        diff: dict[str, Any] = {"files": [], "total": {"add": 0, "del": 0, "files": 0}, "short_stat": ""}
        if state and state.session:
            diff = await diff_summary(Path(state.workspace), state.initialSha, state.artifact.sha if state.artifact else "")
        recent_feedback: list[dict[str, Any]] = []
        for other in self.repo.query(
            "SELECT id, task_name FROM tasks WHERE id != ? AND review_acked_at IS NOT NULL ORDER BY review_acked_at DESC LIMIT 3",
            (task_id,),
        ):
            other_task = self.repo.get_task(str(other["id"]))
            if not other_task:
                continue
            path = Path(str(other_task["task_root"])) / "delivery" / "feedback.md"
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            recent_feedback.append({"task_id": other["id"], "task_name": other["task_name"], "head": text[:180]})
        return {
            "audit": audit,
            "task": task,
            "steps": [step.as_dict() for step in evaluation.steps],
            "state": envelope.raw,
            "state_error": envelope.error,
            "prompt": self.prompts.get(task_id),
            "feedback": self.feedback_service.get(task_id),
            "timeline": {key: value for key, value in trace.items() if not key.startswith("_")},
            "diff": diff,
            "recording": await self.recording.verify(task_id),
            "recent_feedback": recent_feedback,
            "readonly": True,
        }

    async def decide(
        self,
        task_id: str,
        *,
        verdict: str,
        reason: str = "",
        disposition: str = "",
    ) -> dict[str, Any]:
        if verdict not in {"pass", "reject"}:
            raise ApiError(400, "AUDIT_VERDICT_INVALID", "抽检结论只能是 pass 或 reject")
        if verdict == "reject":
            reason = reason.strip()
            if not reason:
                raise ApiError(400, "AUDIT_REASON_REQUIRED", "打回必须填写原因")
            if disposition not in {"redo_feedback", "manual", "void"}:
                raise ApiError(400, "AUDIT_DISPOSITION_REQUIRED", "打回必须选择处置方式")
        task = self.repo.get_task(task_id)
        if not task:
            raise ApiError(404, "TASK_NOT_FOUND", f"任务不存在：{task_id}")
        current = self.repo.audit(task_id) or self.assess(task_id, str(task.get("task_type") or ""))
        if not current.get("sampled"):
            raise ApiError(409, "AUDIT_NOT_SAMPLED", "该任务未被抽中，不能提交抽检结论")
        if current.get("verdict"):
            raise ApiError(409, "AUDIT_ALREADY_DECIDED", "该任务已有抽检结论")
        record = {
            **current,
            "verdict": verdict,
            "reason": reason,
            "disposition": disposition,
            "decided_at": _now(),
        }
        self.repo.upsert_audit(task_id, record)
        self._write(task_id, record)
        self.breaker.record_audit(verdict)
        self.repo.add_event(
            "audit.passed" if verdict == "pass" else "audit.rejected",
            "人工抽检通过" if verdict == "pass" else f"人工抽检打回：{reason}",
            level="info" if verdict == "pass" else "warn",
            actor="user",
            task_id=task_id,
            details={"disposition": disposition},
        )
        self.bus.publish("audit.updated", {"task_id": task_id, "verdict": verdict})
        if verdict == "pass" and self.autopilot:
            job = self.autopilot.jobs.get(task_id)
            if job:
                self.autopilot.mark_done(task_id, "人工抽检通过")
        elif verdict == "reject" and disposition == "redo_feedback":
            ack = Path(task["task_root"]) / "monitor" / "review-ack.json"
            ack.unlink(missing_ok=True)
            self.repo.execute("UPDATE tasks SET ack_by='', updated_at=? WHERE id=?", (_now(), task_id))
            pending = {
                **record,
                "verdict": None,
                "reason": "",
                "disposition": "",
                "decided_at": "",
            }
            self.repo.upsert_audit(task_id, pending)
            self._write(task_id, {**pending, "previousVerdict": "reject", "previousReason": reason})
            if self.autopilot:
                job = self.autopilot.jobs.get(task_id)
                if job:
                    job.redo_feedback = True
                    job.reconfirm_required = True
                await self.autopilot.retry(
                    task_id,
                    from_stage="chatgpt"
                    if self.autopilot.settings.agent_mode == "chatgpt"
                    else "delivering",
                )
        elif verdict == "reject" and disposition == "void":
            self.repo.set_job_status(task_id, "aborted", notes=f"抽检打回作废：{reason}")
            if self.autopilot and self.autopilot.ledger:
                self.autopilot.ledger.mark_outcome(task_id, "void")
            if self.autopilot:
                self.autopilot.mark_failed(task_id, f"人工抽检作废：{reason}")
        elif verdict == "reject" and disposition == "manual":
            if self.autopilot:
                self.autopilot.mark_failed(task_id, f"人工抽检打回，转人工处理：{reason}")
        return record

    def summary(self, start: str = "", end: str = "") -> dict[str, Any]:
        audits = self.repo.audits()
        rows = [row for row in audits if row.get("sampled")]
        if start or end:
            rows = [
                row
                for row in rows
                if (not start or str(row.get("decided_at") or "")[:10] >= start)
                and (not end or str(row.get("decided_at") or "")[:10] <= end)
            ]
        decided = [row for row in rows if row.get("verdict")]
        passed = sum(row.get("verdict") == "pass" for row in decided)
        return {
            "sampled": len(rows),
            "decided": len(decided),
            "passed": passed,
            "rejected": len(decided) - passed,
            "pass_rate": round(passed / max(1, len(decided)), 4),
        }

    def quality_summary(self, start: str = "", end: str = "") -> dict[str, Any]:
        summaries = self.summary(start, end)
        tasks = self.repo.query("SELECT * FROM tasks")
        scores: Counter[str] = Counter()
        lengths: list[int] = []
        ack: Counter[str] = Counter()
        for task in tasks:
            created = str(task.get("created_at") or "")[:10]
            if (start and created and created < start) or (end and created and created > end):
                continue
            score = task.get("feedback_score")
            if score is not None:
                scores[str(score)] += 1
            ack[str(task.get("ack_by") or "unacked")] += 1
            path = Path(str(task["task_root"])) / "delivery" / "feedback.md"
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            lengths.append(len("".join(text.split())))
        failures = Counter(
            str(row.get("failure_kind") or "")
            for row in self.repo.query("SELECT failure_kind FROM autopilot_stages WHERE failure_kind != ''")
            if row.get("failure_kind")
        )
        max_score = max(scores.values(), default=0)
        concentration = max_score / max(1, sum(scores.values()))
        return {
            "audit": summaries,
            "scores": [{"score": key, "count": value} for key, value in sorted(scores.items())],
            "feedback_lengths": {
                "min": min(lengths, default=0),
                "max": max(lengths, default=0),
                "average": round(sum(lengths) / len(lengths), 1) if lengths else 0,
                "samples": len(lengths),
            },
            "failures": [{"kind": key, "count": value} for key, value in failures.most_common()],
            "acknowledgement": dict(ack),
            "warning": "分数过度集中，请人工检查 Feedback 是否模板化" if concentration >= 0.8 and sum(scores.values()) >= 3 else "",
            "readonly": True,
        }

    def _write(self, task_id: str, data: dict[str, Any]) -> None:
        self.paths.audits.mkdir(parents=True, exist_ok=True)
        path = self.paths.audits / f"{task_id}.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)


def _now() -> str:
    return datetime.now().astimezone().replace(microsecond=0).isoformat()

