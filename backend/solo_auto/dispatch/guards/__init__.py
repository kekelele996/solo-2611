"""Safe-by-default guard observations."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from solo_auto.dispatch.queue import DispatchItem, DispatchQueue
from solo_auto.settings import DispatchSettings


@dataclass(slots=True)
class GuardHit:
    id: str
    kind: str
    task_id: str
    reason: str
    action: str
    details: dict[str, Any] = field(default_factory=dict)
    ignored: bool = False
    applied: bool = False
    created_at: str = ""

    def view(self) -> dict[str, Any]:
        return asdict(self)


class GuardRunner:
    def __init__(self, queue: DispatchQueue, settings: DispatchSettings) -> None:
        self.queue = queue
        self.settings = settings
        self.hits: dict[str, GuardHit] = {}

    def scan(self) -> list[GuardHit]:
        mode = self.settings.guard.mode
        if mode == "off":
            return []
        for item in self.queue.all_items(include_terminal=False):
            if item.keep:
                continue
            # The managed skill owns no-progress and deadline decisions.  A
            # session waiting for a retry window or human action must not be
            # counted as an idle or zombie task by the workbench.
            if item.session_state in {"waiting_continue", "holding", "manual_input"}:
                continue
            if item.total_attempts >= self.settings.guard.attempt_inflation:
                self._hit(
                    item,
                    kind="attempt_inflation",
                    reason=f"尝试计数达到 {item.total_attempts}，疑似异常膨胀",
                    action="隔离并告警",
                )
            if item.status == "quarantined" and not item.last_failure:
                self._hit(
                    item,
                    kind="manual_review",
                    reason="任务已隔离但没有可展示的失败证据",
                    action="保留并等待人工补证据",
                )
        return list(self.hits.values())

    def apply(self, hit_id: str) -> GuardHit | None:
        hit = self.hits.get(hit_id)
        if hit is None or hit.ignored:
            return None
        if not require_enforce(self.settings.guard.mode):
            return hit
        if hit.kind == "attempt_inflation":
            self.queue.update(
                hit.task_id,
                status="quarantined",
                keep=True,
                wait_code="guard.attempt_inflation",
                wait_text=hit.reason,
            )
        hit.applied = True
        return hit

    def dismiss(self, hit_id: str) -> GuardHit | None:
        hit = self.hits.get(hit_id)
        if hit:
            hit.ignored = True
        return hit

    def drop(self, task_id: str) -> None:
        for hit_id in [key for key, hit in self.hits.items() if hit.task_id == task_id]:
            del self.hits[hit_id]

    def _hit(self, item: DispatchItem, *, kind: str, reason: str, action: str) -> GuardHit:
        hit_id = f"{kind}:{item.task_id}"
        existing = self.hits.get(hit_id)
        if existing:
            return existing
        hit = GuardHit(
            id=hit_id,
            kind=kind,
            task_id=item.task_id,
            reason=reason,
            action=action,
            created_at=datetime.now(UTC).replace(microsecond=0).isoformat(),
        )
        self.hits[hit_id] = hit
        return hit


def require_enforce(mode: str) -> bool:
    return mode == "enforce"
