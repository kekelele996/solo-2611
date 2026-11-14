"""Persistent, scoped holds.

Each source can own at most one active hold. Automatic recovery may only remove
the hold created by that same source; manual pauses are stored separately by the
dispatcher and are never modified here.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from solo_auto.dispatch.storage import read_json, write_json

# 已下线的守护来源：旧进程留下的持久化挂起在重启时直接丢弃。
DEPRECATED_HOLD_SOURCES = frozenset({"backpressure"})


def _now() -> datetime:
    return datetime.now().astimezone()


def _iso(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat()


def _parse(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.astimezone()


@dataclass(slots=True)
class Hold:
    id: str
    source: str
    scope: list[str]
    reason_code: str
    reason: str
    since: str
    auto: bool = True
    next_at: str = ""
    count: int = 0
    last_error: str = ""
    self_heal_deadline: str = ""
    caused_by_task: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def scopes(self) -> frozenset[str]:
        return frozenset(self.scope)


class HoldRegistry:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.holds: dict[str, Hold] = {}
        self.history: list[dict[str, Any]] = []
        self._load()

    def register(
        self,
        *,
        source: str,
        scope: Iterable[str],
        reason_code: str,
        reason: str,
        probe_seconds: int | None = None,
        self_heal_seconds: int | None = None,
        caused_by_task: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> Hold:
        now = _now()
        existing = self.holds.get(source)
        if existing:
            existing.scope = _unique(scope)
            existing.reason_code = reason_code
            existing.reason = reason
            existing.auto = True
            existing.last_error = str((metadata or {}).get("error") or "")
            existing.caused_by_task = caused_by_task
            existing.metadata = metadata or {}
            existing.count += 1
            if probe_seconds is not None:
                existing.next_at = _iso(now + timedelta(seconds=probe_seconds))
            if self_heal_seconds is not None and not existing.self_heal_deadline:
                existing.self_heal_deadline = _iso(now + timedelta(seconds=self_heal_seconds))
            self._save()
            return existing
        hold = Hold(
            id=source,
            source=source,
            scope=_unique(scope),
            reason_code=reason_code,
            reason=reason,
            since=_iso(now),
            auto=True,
            next_at=_iso(now + timedelta(seconds=probe_seconds)) if probe_seconds is not None else "",
            count=1,
            last_error=str((metadata or {}).get("error") or ""),
            self_heal_deadline=(
                _iso(now + timedelta(seconds=self_heal_seconds)) if self_heal_seconds is not None else ""
            ),
            caused_by_task=caused_by_task,
            metadata=metadata or {},
        )
        self.holds[source] = hold
        self._save()
        return hold

    def release(self, source: str, *, actor: str = "system") -> Hold | None:
        hold = self.holds.pop(source, None)
        if hold is None:
            return None
        ended = _now()
        started = _parse(hold.since)
        duration = max(0, int((ended - started).total_seconds())) if started else 0
        self.history.append(
            {
                **asdict(hold),
                "released_at": _iso(ended),
                "released_by": actor,
                "duration_seconds": duration,
            }
        )
        self.history = self.history[-200:]
        self._save()
        return hold

    def release_id(self, hold_id: str, *, actor: str = "user") -> Hold | None:
        hold = self.holds.get(hold_id)
        return self.release(hold.source, actor=actor) if hold else None

    def active(self, *, scope: str | None = None) -> list[Hold]:
        values = list(self.holds.values())
        if scope is not None:
            values = [hold for hold in values if scope in hold.scope or "global" in hold.scope]
        return sorted(values, key=lambda hold: (hold.since, hold.source))

    def blocking(self, scopes: Iterable[str]) -> Hold | None:
        wanted = set(scopes)
        for hold in self.active():
            if "global" in hold.scope or wanted.intersection(hold.scope):
                return hold
        return None

    def due(self, now: datetime | None = None) -> list[Hold]:
        current = now or _now()
        result: list[Hold] = []
        for hold in self.holds.values():
            next_at = _parse(hold.next_at)
            # 所有挂起都自动复测，没有“等人处理”的挂起。
            if next_at is None or next_at <= current:
                result.append(hold)
        return result

    def mark_probe(self, source: str, *, next_seconds: int, error: str = "") -> None:
        hold = self.holds.get(source)
        if hold is None:
            return
        hold.count += 1
        hold.last_error = error
        hold.next_at = _iso(_now() + timedelta(seconds=next_seconds))
        self._save()

    def update_scope(self, source: str, scope: Iterable[str]) -> None:
        hold = self.holds.get(source)
        if hold:
            hold.scope = _unique(scope)
            self._save()

    def state(self) -> dict[str, Any]:
        return {
            "holds": [asdict(hold) for hold in self.active()],
            "history": self.history,
        }

    def _save(self) -> None:
        write_json(self.path, self.state())

    def _load(self) -> None:
        data = read_json(self.path, {})
        if not isinstance(data, dict):
            return
        for raw in data.get("holds") or []:
            if not isinstance(raw, dict) or not raw.get("source"):
                continue
            known = set(Hold.__dataclass_fields__)
            try:
                hold = Hold(**{key: value for key, value in raw.items() if key in known})
            except TypeError:
                continue
            if hold.source in DEPRECATED_HOLD_SOURCES:
                continue
            hold.auto = True
            self.holds[hold.source] = hold
        self.history = [
            item for item in data.get("history") or [] if isinstance(item, dict)
        ][-200:]


def _unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(str(item) for item in values if str(item)))
