"""SQLite-backed dispatch queue."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from solo_auto.store.repo import Repository

TERMINAL = {"done", "failed", "removed"}
WAITABLE = {"queued", "launching", "active", "backoff", "interrupted", "quarantined"}
IN_HAND = {"queued", "launching", "active", "backoff", "interrupted"}


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


@dataclass(slots=True)
class DispatchItem:
    task_id: str
    status: str = "queued"
    stage: str = "pending"
    priority: int = 0
    position: float = 0.0
    enqueued_at: str = field(default_factory=_now)
    not_before: str = ""
    wait_code: str = ""
    wait_text: str = ""
    wait_until: str = ""
    attempts: dict[str, int] = field(default_factory=dict)
    total_attempts: int = 0
    last_failure: dict[str, Any] = field(default_factory=dict)
    repeat_signature: str = ""
    guard_flags: list[str] = field(default_factory=list)
    keep: bool = False
    plan_id: str = ""
    project_id: str = ""
    engineering_id: str = ""
    session_state: str = ""
    continue_used: int = 0
    next_action_at: str = ""
    container_seconds: int = 0
    updated_at: str = field(default_factory=_now)

    @property
    def active(self) -> bool:
        return self.status in {"active", "launching"}

    def view(self, *, queue_position: int | None = None) -> dict[str, Any]:
        return {
            **asdict(self),
            "active": self.active,
            "queue_position": queue_position,
        }


class DispatchQueue:
    def __init__(self, repo: Repository) -> None:
        self.repo = repo

    def all_items(self, *, include_terminal: bool = True) -> list[DispatchItem]:
        rows = self.repo.query("SELECT * FROM dispatch_items ORDER BY updated_at DESC")
        items = [_from_row(row) for row in rows]
        return items if include_terminal else [item for item in items if item.status not in TERMINAL]

    def get(self, task_id: str) -> DispatchItem | None:
        row = self.repo.query_one("SELECT * FROM dispatch_items WHERE task_id = ?", (task_id,))
        return _from_row(row) if row else None

    def upsert(self, item: DispatchItem) -> DispatchItem:
        item.updated_at = _now()
        self.repo.execute(
            """
            INSERT INTO dispatch_items(
              task_id, status, stage, priority, position, enqueued_at, not_before,
              wait_code, wait_text, wait_until, attempts, total_attempts, last_failure,
              repeat_signature, guard_flags, keep, plan_id, project_id, engineering_id,
              session_state, continue_used, next_action_at, container_seconds, updated_at
            ) VALUES(
              :task_id, :status, :stage, :priority, :position, :enqueued_at, :not_before,
              :wait_code, :wait_text, :wait_until, :attempts, :total_attempts, :last_failure,
              :repeat_signature, :guard_flags, :keep, :plan_id, :project_id, :engineering_id,
              :session_state, :continue_used, :next_action_at, :container_seconds, :updated_at
            )
            ON CONFLICT(task_id) DO UPDATE SET
              status=excluded.status, stage=excluded.stage, priority=excluded.priority,
              position=excluded.position, not_before=excluded.not_before,
              wait_code=excluded.wait_code, wait_text=excluded.wait_text, wait_until=excluded.wait_until,
              attempts=excluded.attempts, total_attempts=excluded.total_attempts,
              last_failure=excluded.last_failure, repeat_signature=excluded.repeat_signature,
              guard_flags=excluded.guard_flags, keep=excluded.keep, plan_id=excluded.plan_id,
              project_id=excluded.project_id, engineering_id=excluded.engineering_id,
              session_state=excluded.session_state, continue_used=excluded.continue_used,
              next_action_at=excluded.next_action_at, container_seconds=excluded.container_seconds,
              updated_at=excluded.updated_at
            """,
            _params(item),
        )
        return item

    def enqueue(
        self,
        task_id: str,
        *,
        stage: str = "pending",
        plan_id: str = "",
        project_id: str = "",
        engineering_id: str = "",
        priority: int = 0,
        front: bool = False,
    ) -> DispatchItem:
        existing = self.get(task_id)
        if existing and existing.status not in TERMINAL:
            existing.stage = stage or existing.stage
            existing.plan_id = plan_id or existing.plan_id
            existing.project_id = project_id or existing.project_id
            existing.engineering_id = engineering_id or existing.engineering_id
            existing.priority = max(existing.priority, priority)
            if front:
                existing.position = self._next_position(front=True)
            return self.upsert(existing)
        return self.upsert(
            DispatchItem(
                task_id=task_id,
                stage=stage,
                plan_id=plan_id,
                project_id=project_id,
                engineering_id=engineering_id,
                priority=priority,
                position=self._next_position(front=front),
            )
        )

    def update(self, task_id: str, **values: Any) -> DispatchItem | None:
        item = self.get(task_id)
        if item is None:
            return None
        allowed = set(DispatchItem.__dataclass_fields__) - {"task_id"}
        for key, value in values.items():
            if key in allowed:
                setattr(item, key, value)
        return self.upsert(item)

    def remove(self, task_id: str, *, status: str = "removed") -> DispatchItem | None:
        item = self.get(task_id)
        if item is None:
            return None
        item.status = status
        item.wait_code = ""
        item.wait_text = ""
        item.wait_until = ""
        return self.upsert(item)

    def ignore(self, task_id: str) -> DispatchItem | None:
        """Close a failed item without retrying; the failure reason stays visible."""
        item = self.get(task_id)
        if item is None:
            return None
        item.status = "removed"
        item.wait_code = "ignored"
        item.wait_until = ""
        item.not_before = ""
        return self.upsert(item)

    def clear_finished(self) -> int:
        return self.repo.execute(
            "DELETE FROM dispatch_items WHERE status IN ('done', 'failed', 'removed')"
        )

    def ordered(self, *, include_unavailable: bool = False) -> list[DispatchItem]:
        items = [item for item in self.all_items(include_terminal=False) if item.status in WAITABLE]
        if not include_unavailable:
            items = [item for item in items if item.status not in {"quarantined"}]
        return sorted(
            items,
            key=lambda item: (
                item.status == "quarantined",
                -item.priority,
                item.status not in {"active", "launching", "interrupted"},
                item.not_before or "",
                item.position,
                item.enqueued_at,
            ),
        )

    def due_retry(self, now: datetime | None = None) -> list[DispatchItem]:
        current = (now or datetime.now(UTC)).timestamp()
        result: list[DispatchItem] = []
        for item in self.all_items(include_terminal=False):
            if item.status != "backoff":
                continue
            if not item.not_before:
                result.append(item)
                continue
            try:
                stamp = datetime.fromisoformat(item.not_before.replace("Z", "+00:00")).timestamp()
            except ValueError:
                stamp = 0
            if stamp <= current:
                result.append(item)
        return result

    def migrate(
        self,
        autopilot_state: dict[str, Any] | None,
        intake_state: dict[str, Any] | None,
    ) -> int:
        count = 0
        plan = (intake_state or {}).get("plan") if isinstance(intake_state, dict) else None
        plan_id = str(plan.get("id") or "") if isinstance(plan, dict) else ""
        plan_project = str(plan.get("project_id") or "") if isinstance(plan, dict) else ""
        for raw in (autopilot_state or {}).get("jobs") or []:
            if not isinstance(raw, dict) or not raw.get("task_id"):
                continue
            stage = str(raw.get("stage") or "pending")
            if stage in {"done", "awaiting_audit"} or raw.get("ignored"):
                continue
            status = "quarantined" if stage in {"failed", "stopped"} else "queued"
            self.enqueue(
                str(raw["task_id"]),
                stage=stage,
                plan_id=plan_id,
                project_id=plan_project,
            )
            self.update(str(raw["task_id"]), status=status)
            count += 1
        return count

    def _next_position(self, *, front: bool = False) -> float:
        if front:
            row = self.repo.query_one("SELECT MIN(position) AS value FROM dispatch_items")
            value = float((row or {}).get("value") or 0.0)
            return value - 10.0
        row = self.repo.query_one("SELECT MAX(position) AS value FROM dispatch_items")
        value = float((row or {}).get("value") or 0.0)
        return value + 10.0


def _from_row(row: dict[str, Any]) -> DispatchItem:
    def decode(value: Any, fallback: Any) -> Any:
        try:
            return json.loads(str(value)) if value else fallback
        except json.JSONDecodeError:
            return fallback

    return DispatchItem(
        task_id=str(row["task_id"]),
        status=str(row["status"]),
        stage=str(row["stage"]),
        priority=int(row["priority"]),
        position=float(row["position"]),
        enqueued_at=str(row["enqueued_at"]),
        not_before=str(row.get("not_before") or ""),
        wait_code=str(row.get("wait_code") or ""),
        wait_text=str(row.get("wait_text") or ""),
        wait_until=str(row.get("wait_until") or ""),
        attempts=decode(row.get("attempts"), {}),
        total_attempts=int(row.get("total_attempts") or 0),
        last_failure=decode(row.get("last_failure"), {}),
        repeat_signature=str(row.get("repeat_signature") or ""),
        guard_flags=decode(row.get("guard_flags"), []),
        keep=bool(row.get("keep")),
        plan_id=str(row.get("plan_id") or ""),
        project_id=str(row.get("project_id") or ""),
        engineering_id=str(row.get("engineering_id") or ""),
        session_state=str(row.get("session_state") or ""),
        continue_used=int(row.get("continue_used") or 0),
        next_action_at=str(row.get("next_action_at") or ""),
        container_seconds=int(row.get("container_seconds") or 0),
        updated_at=str(row["updated_at"]),
    )


def _params(item: DispatchItem) -> dict[str, Any]:
    return {
        **asdict(item),
        "attempts": json.dumps(item.attempts, ensure_ascii=False),
        "last_failure": json.dumps(item.last_failure, ensure_ascii=False),
        "guard_flags": json.dumps(item.guard_flags, ensure_ascii=False),
    }


def task_ids(items: Iterable[DispatchItem]) -> list[str]:
    return [item.task_id for item in items]
