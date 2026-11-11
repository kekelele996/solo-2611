"""Persistent delivery and audit circuit breaker."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from solo_auto.events.bus import EventBus
from solo_auto.pipeline.notify import Notifier
from solo_auto.settings import AutopilotSettings


def _now() -> str:
    return datetime.now().astimezone().replace(microsecond=0).isoformat()


class Breaker:
    def __init__(self, path: Path, bus: EventBus, settings: AutopilotSettings) -> None:
        self.path = path
        self.bus = bus
        self.settings = settings
        self.tripped = False
        self.kind = ""
        self.since = ""
        self.consecutive_kind = ""
        self.consecutive_count = 0
        self.audit_verdicts: list[str] = []
        self.notifier: Notifier | None = None
        self._load()

    def attach_notifier(self, notifier: Notifier) -> None:
        self.notifier = notifier

    @property
    def state(self) -> dict[str, Any]:
        return {
            "tripped": self.tripped,
            "kind": self.kind,
            "since": self.since,
            "consecutive_kind": self.consecutive_kind,
            "consecutive_count": self.consecutive_count,
            "audit_verdicts": self.audit_verdicts[-self.settings.breaker.audit_reject_window :],
        }

    def record_failure(self, kind: str, detail: str) -> bool:
        if kind == self.consecutive_kind:
            self.consecutive_count += 1
        else:
            self.consecutive_kind = kind
            self.consecutive_count = 1
        should_trip = kind in {"auth", "environment"} or (
            self.settings.enabled and self.consecutive_count >= self.settings.breaker.same_kind_failures
        )
        if should_trip:
            self.trip(kind, detail)
        else:
            self._save()
        return should_trip

    def record_success(self) -> None:
        self.consecutive_kind = ""
        self.consecutive_count = 0
        self._save()

    def record_audit(self, verdict: str) -> bool:
        self.audit_verdicts.append(verdict)
        self.audit_verdicts = self.audit_verdicts[-self.settings.breaker.audit_reject_window :]
        rejects = self.audit_verdicts.count("reject")
        tripped = rejects >= 3 and rejects / len(self.audit_verdicts) >= self.settings.breaker.audit_reject_rate
        if tripped:
            self.trip("audit_reject", f"最近 {len(self.audit_verdicts)} 次抽检打回 {rejects} 次")
        else:
            self._save()
        return tripped

    def trip(self, kind: str, detail: str) -> None:
        if self.tripped:
            return
        self.tripped = True
        self.kind = kind
        self.since = _now()
        self._save()
        self.bus.publish(
            "autopilot.breaker",
            {"tripped": True, "kind": kind, "since": self.since, "detail": detail},
        )
        self.bus.publish(
            "toast",
            {"level": "error", "message": f"自动交付已熔断：{detail}"},
        )
        if self.notifier:
            self.notifier.notify(title="solo-auto 已熔断", status=f"{kind}：{detail}")

    def reset(self) -> None:
        self.tripped = False
        self.kind = ""
        self.since = ""
        self.consecutive_kind = ""
        self.consecutive_count = 0
        self._save()
        self.bus.publish("autopilot.breaker", {"tripped": False})

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.path)

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        self.tripped = bool(data.get("tripped"))
        self.kind = str(data.get("kind") or "")
        self.since = str(data.get("since") or "")
        self.consecutive_kind = str(data.get("consecutive_kind") or "")
        self.consecutive_count = int(data.get("consecutive_count") or 0)
        self.audit_verdicts = [str(item) for item in data.get("audit_verdicts") or []]

