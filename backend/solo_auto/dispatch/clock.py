"""Wall-clock and monotonic clock with sleep and fault injection support."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


class ClockProtocol(Protocol):
    def wall(self) -> datetime: ...

    def monotonic(self) -> float: ...


class Clock:
    def wall(self) -> datetime:
        return datetime.now().astimezone()

    def monotonic(self) -> float:
        return time.monotonic()


class VirtualClock:
    """Deterministic clock used by unit and soak tests."""

    def __init__(self, wall: datetime | None = None, monotonic: float = 1000.0) -> None:
        self._wall = wall or datetime(2026, 11, 11, 12, 0, tzinfo=datetime.now().astimezone().tzinfo)
        self._monotonic = monotonic

    def wall(self) -> datetime:
        return self._wall

    def monotonic(self) -> float:
        return self._monotonic

    def advance(self, seconds: float) -> None:
        from datetime import timedelta

        self._wall += timedelta(seconds=seconds)
        self._monotonic += seconds

    def sleep(self, seconds: float) -> None:
        self.advance(seconds)


@dataclass(slots=True)
class ClockState:
    wall_seconds: float
    monotonic_seconds: float
    slept_seconds: float = 0.0
    jumped_back: bool = False


def observe_clock(previous: ClockState | None, clock: ClockProtocol) -> ClockState:
    wall = clock.wall().timestamp()
    monotonic = clock.monotonic()
    if previous is None:
        return ClockState(wall, monotonic)
    wall_delta = wall - previous.wall_seconds
    monotonic_delta = monotonic - previous.monotonic_seconds
    slept = max(0.0, wall_delta - monotonic_delta) if wall_delta > monotonic_delta + 30 else 0.0
    return ClockState(
        wall_seconds=wall,
        monotonic_seconds=monotonic,
        slept_seconds=previous.slept_seconds + slept,
        jumped_back=wall_delta < -1,
    )
