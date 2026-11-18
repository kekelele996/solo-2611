"""Rate-limit-aware elastic container ceiling.

``dispatch.containers`` is the operator-set floor.  When elasticity is enabled,
the dispatcher may probe one extra container at a time while containers are
full and work is waiting.  The limit steps back down only when 429 retry waits
actually consume a meaningful share of container time, rather than merely
counting 429 log lines.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from solo_auto.dispatch.storage import read_json, write_json
from solo_auto.settings import DispatchElasticSettings, DispatchSettings

RATE_LIMIT_WINDOW_SECONDS = 300
RATE_LIMIT_READ_CAP = 4 * 1024 * 1024
LEARNED_TTL_SECONDS = 7 * 86400


@dataclass(slots=True)
class ElasticState:
    limit: int = 0
    last_up_at: float = 0.0
    last_down_at: float = 0.0
    last_change_at: float = 0.0
    last_pressure_at: float = 0.0
    last_429_at: float = 0.0
    hour: int = -1
    last_reason: str = ""
    hourly_memory: dict[str, dict[str, float]] = field(default_factory=dict)


@dataclass(slots=True)
class ElasticAdjustment:
    action: str
    previous: int
    current: int
    reason: str


def parse_retry_events(chunk: bytes) -> list[tuple[int, float]]:
    """Return ``(attempt, retry wait seconds)`` from stream-json retry events."""

    found: list[tuple[int, float]] = []
    for raw in chunk.splitlines():
        if b"api_retry" not in raw and b"retry_delay_ms" not in raw:
            continue
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(value, dict):
            continue
        candidates = [value]
        for key in ("event", "data", "message", "error"):
            nested = value.get(key)
            if isinstance(nested, dict):
                candidates.append(nested)
        for event in candidates:
            kind = f"{event.get('type') or ''} {event.get('subtype') or ''}".strip()
            status = str(event.get("error_status") or event.get("status") or "")
            if "api_retry" not in kind or status != "429":
                continue
            try:
                attempt = int(event.get("attempt") or 1)
            except (TypeError, ValueError):
                attempt = 1
            try:
                wait = max(0.0, float(event.get("retry_delay_ms") or 0) / 1000)
            except (TypeError, ValueError):
                wait = 0.0
            found.append((attempt, wait))
            break
    return found


class ElasticController:
    def __init__(self, path: Path, settings: DispatchSettings) -> None:
        self.path = path
        self.settings = settings
        self.state = self._load()
        self._offsets: dict[str, int] = {}
        self._watch_since = time.time()
        # (seen at, attempt, retry wait seconds).  These are process-local; the
        # durable state only keeps the resulting limit and timing metadata.
        self._retry_hits: list[tuple[float, int, float]] = []

    @property
    def config(self) -> DispatchElasticSettings:
        return self.settings.elastic

    @property
    def floor(self) -> int:
        return self.settings.containers

    @property
    def ceiling(self) -> int:
        return max(self.floor, self.config.ceiling)

    @property
    def effective(self) -> int:
        if not self.config.enabled:
            return self.floor
        return min(self.ceiling, max(self.floor, self.state.limit or self.floor))

    def update_settings(self, settings: DispatchSettings) -> None:
        self.settings = settings

    def scan(self, files: Iterable[Path], *, now: float | None = None) -> list[tuple[int, float]]:
        """Read retry events appended to active candidate traces since the last scan."""

        current = now if now is not None else time.time()
        found: list[tuple[int, float]] = []
        seen: set[str] = set()
        for path in files:
            key = str(path)
            seen.add(key)
            try:
                stat = path.stat()
            except OSError:
                continue
            offset = self._offsets.get(key)
            if offset is None:
                born = getattr(stat, "st_birthtime", stat.st_ctime)
                offset = 0 if born >= self._watch_since - 1 else stat.st_size
            if stat.st_size < offset:
                offset = 0
            if stat.st_size <= offset:
                self._offsets[key] = offset
                continue
            start = max(offset, stat.st_size - RATE_LIMIT_READ_CAP)
            try:
                with path.open("rb") as handle:
                    handle.seek(start)
                    chunk = handle.read(stat.st_size - start)
            except OSError:
                continue
            end = chunk.rfind(b"\n")
            if end >= 0:
                found.extend(parse_retry_events(chunk[: end + 1]))
                offset = start + end + 1
            self._offsets[key] = offset
        for key in list(self._offsets):
            if key not in seen:
                self._offsets.pop(key, None)
        # ``current`` is accepted so callers can drive the scan with the same
        # clock as the dispatcher; offsets themselves are clock-independent.
        _ = current
        return found

    def observe(self, retries: Iterable[tuple[int, float]], *, now: float) -> None:
        hits = [(now, attempt, wait) for attempt, wait in retries]
        if not hits:
            return
        self._retry_hits.extend(hits)
        self.state.last_429_at = now
        self._prune_hits(now)
        self._save()

    def note_severe_rate_limit(self, *, now: float, wait_seconds: float = 0.0) -> None:
        """Record a terminal 429 without requiring an api_retry trace entry."""

        self.observe([(self.config.severe_attempt, wait_seconds)], now=now)

    def _prune_hits(self, now: float) -> None:
        self._retry_hits = [
            hit for hit in self._retry_hits if now - hit[0] <= RATE_LIMIT_WINDOW_SECONDS
        ]

    def pressure(self, *, now: float, limit: int | None = None) -> tuple[float, int, int]:
        self._prune_hits(now)
        wait = sum(hit[2] for hit in self._retry_hits)
        worst = max((hit[1] for hit in self._retry_hits), default=0)
        effective = max(1, limit or self.effective)
        return wait / (effective * RATE_LIMIT_WINDOW_SECONDS), worst, len(self._retry_hits)

    def learned_limit(self, now: float) -> int:
        entry = self.state.hourly_memory.get(str(self._local_hour(now)))
        if not isinstance(entry, dict):
            return 0
        try:
            at = float(entry.get("at") or 0)
            limit = int(entry.get("limit") or 0)
        except (TypeError, ValueError):
            return 0
        return limit if now - at <= LEARNED_TTL_SECONDS else 0

    def _learn(self, *, now: float, limit: int, lower: bool = False) -> None:
        hour = str(self._local_hour(now))
        previous = self.learned_limit(now)
        if not lower and limit < previous:
            return
        entry = self.state.hourly_memory.get(hour)
        at = float(entry.get("at") or 0) if isinstance(entry, dict) else 0.0
        if not lower and limit == previous and now - at < 3600:
            return
        self.state.hourly_memory[hour] = {"limit": float(limit), "at": now}
        # Keep a bounded history if the machine clock jumps around.
        if len(self.state.hourly_memory) > 168:
            newest = sorted(
                self.state.hourly_memory.items(), key=lambda item: float(item[1].get("at") or 0)
            )[-168:]
            self.state.hourly_memory = dict(newest)
        self._save()

    @staticmethod
    def _local_hour(now: float) -> int:
        return datetime.fromtimestamp(now).astimezone().hour

    def adjust(
        self,
        *,
        now: float,
        full: bool,
        waiting: bool,
    ) -> ElasticAdjustment | None:
        if not self.config.enabled:
            return None
        current = self.effective
        stall, worst, count = self.pressure(now=now, limit=current)
        threshold = self.config.stall_percent / 100
        severe = worst >= self.config.severe_attempt

        if stall >= threshold or severe:
            previous_pressure = self.state.last_pressure_at
            self.state.last_pressure_at = now
            if now - self.state.last_down_at < 180:
                self._save()
                return None
            cause = f"近 5 分钟 {count} 次 429，重试等待占容器时间 {stall * 100:.1f}%"
            if severe:
                cause += f"，单次请求重试到第 {worst} 次"
            if current > self.floor:
                self._learn(now=now, limit=current - 1, lower=True)
                self._retry_hits.clear()
                return self._move(
                    now=now,
                    action="down",
                    previous=current,
                    target=current - 1,
                    reason=f"{cause}，容器上限 {current} → {current - 1}（保底 {self.floor}）",
                )
            if now - previous_pressure >= self.config.cooldown_seconds or not self.state.last_reason:
                reason = f"{cause}，已在保底 {self.floor} 个容器，保底值可能偏高"
                self.state.last_pressure_at = now
                self.state.last_reason = reason
                self._save()
                return ElasticAdjustment("rate_limited_at_floor", current, current, reason)
            self._save()
            return None

        # A full, quiet limit held long enough is worth remembering for this
        # hour of day, so later runs resume at the known-good value.
        stable_since = max(self.state.last_change_at, self.state.last_pressure_at)
        if full and now - stable_since >= self.config.stable_seconds:
            self._learn(now=now, limit=current)

        hour = self._local_hour(now)
        if self.state.hour != hour:
            self.state.hour = hour
            learned = min(self.learned_limit(now), self.ceiling)
            if (
                learned > current
                and stall < threshold / 2
                and now - self.state.last_pressure_at >= self.config.cooldown_seconds
            ):
                return self._move(
                    now=now,
                    action="resume",
                    previous=current,
                    target=learned,
                    reason=f"本时段此前稳定在 {learned} 个容器，容器上限 {current} → {learned}",
                )
            self._save()

        if (
            full
            and waiting
            and current < self.ceiling
            and stall < threshold / 2
            and now - self.state.last_pressure_at >= self.config.cooldown_seconds
            and now - self.state.last_change_at >= self.config.step_up_seconds
        ):
            quiet = (
                f"近 5 分钟 429 重试等待 {stall * 100:.1f}%"
                if count
                else "近 5 分钟无 429"
            )
            return self._move(
                now=now,
                action="up",
                previous=current,
                target=current + 1,
                reason=f"容器已满且有任务在等，{quiet}，容器上限 {current} → {current + 1}（上限 {self.ceiling}）",
            )
        return None

    def _move(self, *, now: float, action: str, previous: int, target: int, reason: str) -> ElasticAdjustment:
        self.state.limit = target
        self.state.last_change_at = now
        self.state.last_reason = reason
        if action == "down":
            self.state.last_down_at = now
        else:
            self.state.last_up_at = now
        self._save()
        return ElasticAdjustment(action, previous, target, reason)

    def reset(self, *, from_floor: bool = False, now: float | None = None) -> None:
        current = now if now is not None else time.time()
        learned = 0 if from_floor else self.learned_limit(current)
        self.state.limit = self.floor if from_floor or not learned else max(self.floor, learned)
        self.state.hour = self._local_hour(current)
        self.state.last_change_at = current
        self.state.last_pressure_at = 0.0
        self.state.last_reason = "页面保存的保底值" if from_floor else "按当前时段与配置重新开始"
        self._retry_hits.clear()
        self._save()

    def status(self, *, now: float | None = None) -> dict[str, Any]:
        current_time = now if now is not None else time.time()
        current = self.effective
        stall, worst, count = self.pressure(now=current_time, limit=current)
        return {
            "enabled": self.config.enabled,
            "floor": self.floor,
            "ceiling": self.ceiling,
            "current": current,
            "step_up_seconds": self.config.step_up_seconds,
            "cooldown_seconds": self.config.cooldown_seconds,
            "stall_percent": self.config.stall_percent,
            "severe_attempt": self.config.severe_attempt,
            "stable_seconds": self.config.stable_seconds,
            "recent_rate_limits": count,
            "stall_percent_now": round(stall * 100, 1),
            "worst_attempt": worst,
            "learned_for_hour": self.learned_limit(current_time),
            "last_429_at": _iso(self.state.last_429_at),
            "last_pressure_at": _iso(self.state.last_pressure_at),
            "last_change_at": _iso(self.state.last_change_at),
            "last_reason": self.state.last_reason,
        }

    def _load(self) -> ElasticState:
        raw = read_json(self.path, {})
        if not isinstance(raw, dict):
            return ElasticState(limit=self.floor)
        memory = raw.get("hourly_memory") or raw.get("learned") or {}
        hourly: dict[str, dict[str, float]] = {}
        if isinstance(memory, dict):
            for key, value in memory.items():
                if isinstance(value, dict):
                    try:
                        hourly[str(key)] = {
                            "limit": float(value.get("limit") or 0),
                            "at": float(value.get("at") or 0),
                        }
                    except (TypeError, ValueError):
                        continue
        try:
            limit = int(raw.get("limit") or raw.get("effective") or self.floor)
        except (TypeError, ValueError):
            limit = self.floor
        return ElasticState(
            limit=limit,
            last_up_at=_float(raw.get("last_up_at")),
            last_down_at=_float(raw.get("last_down_at")),
            last_change_at=_float(raw.get("last_change_at")),
            last_pressure_at=_float(raw.get("last_pressure_at")),
            last_429_at=_float(raw.get("last_429_at")),
            hour=_int(raw.get("hour"), -1),
            last_reason=str(raw.get("last_reason") or ""),
            hourly_memory=hourly,
        )

    def _save(self) -> None:
        state = asdict(self.state)
        state["floor"] = self.floor
        state["ceiling"] = self.ceiling
        state["effective"] = self.effective
        state["updated_at"] = datetime.now().astimezone().replace(microsecond=0).isoformat()
        try:
            write_json(self.path, state)
        except OSError:
            return


def _float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _iso(timestamp: float) -> str:
    if timestamp <= 0:
        return ""
    return datetime.fromtimestamp(timestamp).astimezone().replace(microsecond=0).isoformat()
