"""Retry, quarantine, cooldown, and hold decisions."""

from __future__ import annotations

import random
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


class RecoveryPolicy:
    def __init__(self, settings: DispatchRetrySettings, *, ramp: random.Random | None = None) -> None:
        self.settings = settings
        self.random = ramp or random.Random()

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
        if failure.disposition in {"quarantine", "manual"}:
            return PolicyDecision("quarantine", "quarantined", detail=failure.detail)
        evidence_stage = str(failure.evidence.get("stage") or item.stage)
        stage_attempts = int(item.attempts.get(evidence_stage, 0))
        charged = stage_attempts + (1 if failure.charge else 0)
        if failure.kind == "agent.launch_lost" and item.total_attempts >= failure.max_retries:
            return PolicyDecision("quarantine", "quarantined", detail="会话拉起连续失败，已隔离")
        if failure.kind == f"unknown.{evidence_stage}" and charged > self.settings.unknown_retries:
            return PolicyDecision("quarantine", "quarantined", detail="未知失败重试已用尽")
        if failure.charge and stage_attempts >= failure.max_retries:
            return PolicyDecision("quarantine", "quarantined", detail="该阶段重试次数已用尽")
        if item.total_attempts + 1 >= self.settings.max_total_attempts:
            return PolicyDecision("quarantine", "quarantined", detail="达到单题总尝试上限")
        if failure.charge:
            index = min(stage_attempts, len(self.settings.backoff_seconds) - 1)
            base = self.settings.backoff_seconds[index]
        else:
            base = self.settings.backoff_seconds[0]
        base = max(1, round(base * max(0.1, failure.backoff_multiplier)))
        jitter = 1 + self.random.uniform(-self.settings.jitter, self.settings.jitter)
        delay = max(1, round(base * jitter))
        not_before = (current + timedelta(seconds=delay)).replace(microsecond=0).isoformat()
        return PolicyDecision(
            action="retry",
            status="backoff",
            delay_seconds=delay,
            not_before=not_before,
            detail=failure.detail,
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
        updates: dict[str, Any] = {
            "status": decision.status,
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
        if decision.status == "quarantined":
            updates.update(
                {
                    "wait_code": "quarantined",
                    "wait_text": decision.detail or failure.kind,
                    "wait_until": "",
                }
            )
        return decision, queue.update(item.task_id, **updates)
