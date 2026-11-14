"""Failure decisions: requeue at the tail, give up, or hold for an automatic probe."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from solo_auto.dispatch.classify import Failure
from solo_auto.dispatch.queue import DispatchItem, DispatchQueue
from solo_auto.settings import DispatchRetrySettings


@dataclass(slots=True)
class PolicyDecision:
    action: str
    status: str
    delay_seconds: int = 0
    not_before: str = ""
    detail: str = ""
    hold_source: str = ""
    hold_scope: list[str] | None = None
    charge: bool = False


# 运行失败统一的处理：记日志、排到队尾，短暂停留后自动重跑；不再退避、隔离或等人处理。
REQUEUE_DELAY_SECONDS = 30
# 会话已经消耗、不能原样重跑的失败：直接结束该题，由自动清理归还工程、在队尾补新题。
GIVE_UP_DISPOSITIONS = frozenset({"quarantine", "manual", "final"})


class RecoveryPolicy:
    def __init__(self, settings: DispatchRetrySettings) -> None:
        self.settings = settings

    def decide(
        self,
        item: DispatchItem,
        failure: Failure,
        *,
        now: datetime | None = None,
    ) -> PolicyDecision:
        current = now or datetime.now(UTC)
        if failure.hold_source:
            return PolicyDecision(
                action="hold",
                status="queued",
                detail=failure.detail,
                hold_source=failure.hold_source,
                hold_scope=failure.hold_scope or ["global"],
            )
        if failure.disposition == "interrupted":
            return PolicyDecision("stop", "interrupted", detail="已中断，可续跑")
        if failure.disposition in {"requeue", "missing"}:
            status = "removed" if failure.disposition == "missing" else "queued"
            return PolicyDecision("requeue", status, detail=failure.detail)
        if failure.disposition in GIVE_UP_DISPOSITIONS:
            return PolicyDecision("give_up", "failed", detail=failure.detail or failure.kind)
        attempts = item.total_attempts + 1
        if attempts >= self.settings.max_total_attempts:
            return PolicyDecision(
                "give_up",
                "failed",
                detail=f"累计失败 {attempts} 次，放弃该题：{failure.detail or failure.kind}",
            )
        not_before = (current + timedelta(seconds=REQUEUE_DELAY_SECONDS)).replace(microsecond=0).isoformat()
        return PolicyDecision(
            action="retry",
            status="backoff",
            delay_seconds=REQUEUE_DELAY_SECONDS,
            not_before=not_before,
            detail=f"第 {attempts} 次失败已记录，排到队尾重跑：{failure.detail or failure.kind}",
            charge=failure.charge,
        )

    def apply(
        self,
        queue: DispatchQueue,
        item: DispatchItem,
        failure: Failure,
        *,
        now: datetime | None = None,
    ) -> tuple[PolicyDecision, DispatchItem | None]:
        decision = self.decide(item, failure, now=now)
        stage = str(failure.evidence.get("stage") or item.stage or "stage")
        attempts = dict(item.attempts)
        if decision.charge:
            attempts[stage] = int(attempts.get(stage, 0)) + 1
        if failure.kind in {"run.start_failed", "run.session_lost"}:
            attempts[failure.kind] = int(attempts.get(failure.kind, 0)) + 1
        updates: dict[str, Any] = {
            "status": decision.status,
            # 重跑的题排到队尾，先让后面的题有机会运行。
            **({"position": queue.next_position(), "priority": 0} if decision.action == "retry" else {}),
            "stage": stage,
            "not_before": decision.not_before,
            "attempts": attempts,
            "total_attempts": item.total_attempts + 1,
            "last_failure": failure.view(),
            "repeat_signature": failure.signature(),
            "wait_code": decision.action if decision.status == "backoff" else failure.kind,
            "wait_text": decision.detail,
            "wait_until": decision.not_before,
        }
        if decision.action == "give_up":
            updates.update({"wait_code": "gave_up", "wait_text": decision.detail, "wait_until": ""})
        return decision, queue.update(item.task_id, **updates)
