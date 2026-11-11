"""Rebuild the SQLite index from the skill-owned disk state."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from solo_auto.dispatch.facts import docker_container_alive
from solo_auto.pipeline.steps import derive_job_status, evaluate_steps
from solo_auto.settings import Paths
from solo_auto.store.repo import Repository
from solo_auto.store.state import SoloState, load_state

LOGGER = logging.getLogger(__name__)
ContainerProbe = Callable[[str], Awaitable[bool]]


class Reconciler:
    def __init__(
        self,
        repo: Repository,
        paths: Paths,
        tasks_dir: Path,
        *,
        container_probe: ContainerProbe | None = None,
    ) -> None:
        self.repo = repo
        self.paths = paths
        self.tasks_dir = tasks_dir
        self.container_probe = container_probe or _docker_container_alive

    async def run(self) -> int:
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        roots = sorted(path.parent.parent for path in self.tasks_dir.glob("*/monitor/state.json"))
        extras = sorted(
            path
            for path in self.tasks_dir.iterdir()
            if path.is_dir() and path not in roots and not path.name.startswith(".")
        )
        all_roots = [*roots, *extras]
        envelopes = {root: load_state(root) for root in all_roots}
        session_counts: dict[str, int] = {}
        for envelope in envelopes.values():
            if envelope.state and envelope.state.session and envelope.state.session.sessionId:
                sid = envelope.state.session.sessionId
                session_counts[sid] = session_counts.get(sid, 0) + 1
        corrected = 0
        live_checks: dict[str, bool] = {}
        for root, envelope in envelopes.items():
            previous = self.repo.get_task_by_root(root)
            if envelope.state is None:
                task_id = self.repo.task_id_for_root(root)
                job_status = "orphan" if not envelope.raw else "blocked"
                record = _record_for_incompatible(task_id, root, envelope.raw, job_status)
                notes = str(previous.get("notes") or "") if previous else None
                self.repo.upsert_task(record, notes=notes)
                eval_result = evaluate_steps(root, duplicate_sessions=session_counts)
                self.repo.replace_steps(task_id, [step.as_dict() for step in eval_result.steps])
                if previous is None:
                    self.repo.add_event(
                        "task.orphan" if job_status == "orphan" else "task.incompatible",
                        envelope.error,
                        level="warn",
                        actor="reconcile",
                        task_id=task_id,
                    )
                continue
            state = envelope.state
            task_id = self.repo.task_id_for_root(root)
            container = _current_container(state)
            if state.status == "running" and container:
                if container not in live_checks:
                    try:
                        live_checks[container] = await self.container_probe(container)
                    except OSError:
                        live_checks[container] = False
                live = live_checks[container]
            else:
                live = False
            ack = _read_json(root / "monitor" / "review-ack.json")
            eval_result = evaluate_steps(root, duplicate_sessions=session_counts)
            job_status = derive_job_status(
                state,
                container_live=live,
                review_acked=bool(ack.get("acked")),
                recording_ok=eval_result.steps[8].ok,
            )
            if previous and previous.get("job_status") in {"aborted", "archived"}:
                job_status = str(previous["job_status"])
            meta = _task_meta(self.paths, root)
            record = _record_for_state(task_id, root, state, job_status, meta, ack)
            if record.get("session_id") and session_counts.get(str(record["session_id"]), 0) > 1:
                record["session_id"] = None
            old_status = str(previous.get("job_status")) if previous else None
            notes = str(previous.get("notes") or "") if previous else None
            self.repo.upsert_task(record, notes=notes)
            self.repo.replace_steps(task_id, [step.as_dict() for step in eval_result.steps])
            if previous and old_status != job_status:
                corrected += 1
                self.repo.add_event(
                    "reconcile.corrected",
                    f"{state.taskName} 状态由 {old_status} 修正为 {job_status}",
                    actor="reconcile",
                    task_id=task_id,
                    details={"old": old_status, "new": job_status, "skill_status": state.status},
                )
        known_roots = {str(root) for root in all_roots}
        for task in self.repo.query("SELECT id, task_root, presence FROM tasks"):
            root = Path(str(task["task_root"]))
            if str(root) not in known_roots and not root.is_dir():
                if task.get("presence") != "missing":
                    self.repo.mark_presence(str(task["id"]), "missing")
                    self.repo.add_event(
                        "task.missing",
                        "任务目录已不存在，记录保留等待人工移除",
                        level="warn",
                        actor="reconcile",
                        task_id=str(task["id"]),
                    )
            elif task.get("presence") != "present":
                self.repo.mark_presence(str(task["id"]), "present")
        return corrected

    async def loop(self, *, interval: float = 60) -> None:
        while True:
            try:
                await self.run()
            except Exception:
                LOGGER.exception("reconcile failed")
            await asyncio.sleep(interval)


def _record_for_state(
    task_id: str,
    root: Path,
    state: SoloState,
    job_status: str,
    meta: dict[str, Any],
    ack: dict[str, Any],
) -> dict[str, Any]:
    fields = state.fields
    return {
        "id": task_id,
        "task_root": str(root),
        "task_name": state.taskName or root.name,
        "source_kind": str(meta.get("source_kind") or state.source.kind or "local_dir"),
        "engineering_id": str(meta.get("engineeringId") or ""),
        "project_id": str(meta.get("projectId") or ""),
        "claim_id": str(meta.get("claimId") or ""),
        "domain": str(fields.get("所属领域") or ""),
        "sub_direction": str(fields.get("子方向") or ""),
        "task_type": str(fields.get("任务类型") or ""),
        "difficulty": str(fields.get("任务难度") or ""),
        "languages": str(fields.get("语言/框架") or ""),
        "repro_level": str(fields.get("环境可复现等级") or ""),
        "job_status": job_status,
        "skill_status": state.status,
        "skill_version": state.skillVersion,
        "initial_sha": state.initialSha,
        "artifact_sha": state.artifact.sha if state.artifact else "",
        "session_id": state.session.sessionId if state.session else None,
        "prompt_id": state.session.promptId if state.session else "",
        "prompt_sha256": state.promptSha256,
        "feedback_score": state.feedbackCheck.score if state.feedbackCheck else None,
        "ack_by": str(ack.get("ackedBy") or ""),
        "review_acked_at": str(ack.get("ackedAt") or ""),
        "exported_at": state.export.exportedAt if state.export else None,
        "created_at": state.createdAt or "",
        "presence": "present",
    }


def _record_for_incompatible(
    task_id: str,
    root: Path,
    raw: dict[str, Any],
    job_status: str,
) -> dict[str, Any]:
    raw_fields = raw.get("fields")
    fields: dict[str, Any] = raw_fields if isinstance(raw_fields, dict) else {}
    return {
        "id": task_id,
        "task_root": str(root),
        "task_name": str(raw.get("taskName") or root.name),
        "source_kind": "unknown",
        "engineering_id": "",
        "project_id": "",
        "claim_id": "",
        "domain": str(fields.get("所属领域") or ""),
        "sub_direction": str(fields.get("子方向") or ""),
        "task_type": str(fields.get("任务类型") or ""),
        "difficulty": str(fields.get("任务难度") or ""),
        "languages": str(fields.get("语言/框架") or ""),
        "repro_level": str(fields.get("环境可复现等级") or ""),
        "job_status": job_status,
        "skill_status": raw.get("status"),
        "skill_version": str(raw.get("skillVersion") or ""),
        "initial_sha": str(raw.get("initialSha") or ""),
        "artifact_sha": "",
        "session_id": None,
        "prompt_id": "",
        "prompt_sha256": str(raw.get("promptSha256") or ""),
        "feedback_score": None,
        "ack_by": "",
        "review_acked_at": "",
        "exported_at": None,
        "created_at": str(raw.get("createdAt") or ""),
        "presence": "present" if root.is_dir() else "missing",
    }


def _task_meta(paths: Paths, root: Path) -> dict[str, Any]:
    for meta_path in paths.staging.glob("*/meta.json"):
        meta = _read_json(meta_path)
        if str(meta.get("taskRoot") or "") == str(root):
            return meta
    task_meta = paths.home / "run" / "task-meta" / f"{root.name}.json"
    return _read_json(task_meta)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _current_container(state: SoloState) -> str:
    if state.pendingContainer:
        return state.pendingContainer
    for attempt in reversed(state.attempts):
        if attempt.status in {"running", "pending_collect"} and attempt.container:
            return attempt.container
    return ""


async def _docker_container_alive(name: str) -> bool:
    return await docker_container_alive(name)
