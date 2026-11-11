"""Recover work after holds or restarts."""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from solo_auto.dispatch.holds import Hold
from solo_auto.dispatch.queue import DispatchQueue


@dataclass(slots=True)
class RecoveryResult:
    requeued: int = 0
    preserved: int = 0
    task_ids: list[str] | None = None

    def view(self) -> dict[str, Any]:
        return {
            "requeued": self.requeued,
            "preserved": self.preserved,
            "task_ids": self.task_ids or [],
        }


class DispatchRecovery:
    def __init__(self, queue: DispatchQueue, *, ramp: random.Random | None = None) -> None:
        self.queue = queue
        self.random = ramp or random.Random()

    def after_hold(self, hold: Hold) -> RecoveryResult:
        matching = [
            item
            for item in self.queue.all_items(include_terminal=False)
            if (
                item.status in {"quarantined", "failed"}
                or (item.status == "queued" and bool(item.last_failure))
            )
            and str((item.last_failure or {}).get("kind") or "").startswith(_family(hold.reason_code))
            and not item.keep
        ]
        now = datetime.now(UTC)
        recovered: list[str] = []
        for item in matching:
            delay = self.random.randint(0, 60)
            attempts = dict(item.attempts)
            stage = str((item.last_failure or {}).get("stage") or item.stage)
            if attempts.get(stage, 0) > 0:
                attempts[stage] -= 1
            self.queue.update(
                item.task_id,
                status="queued",
                attempts=attempts,
                not_before=(now + timedelta(seconds=delay)).replace(microsecond=0).isoformat(),
                wait_code="",
                wait_text="",
                wait_until="",
            )
            recovered.append(item.task_id)
        return RecoveryResult(len(recovered), len(matching) - len(recovered), recovered)


def _family(code: str) -> str:
    return code.split(".", 1)[0] + "."
