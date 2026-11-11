"""429-delay-aware elastic container ceiling."""

from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from solo_auto.dispatch.storage import read_json, write_json
from solo_auto.settings import DispatchSettings

RETRY_RE = re.compile(r"(?i)(?:retry_delay_ms|delay_ms|wait_ms)[\"']?\s*[:=]\s*(\d+)")


@dataclass(slots=True)
class ElasticState:
    effective: int
    floor: int
    ceiling: int
    last_down_at: float = 0.0
    last_up_at: float = 0.0
    last_stable_at: float = 0.0
    hourly_memory: dict[str, int] = field(default_factory=dict)

    def view(self) -> dict[str, Any]:
        return asdict(self)


class ElasticController:
    def __init__(self, path: Path, settings: DispatchSettings) -> None:
        self.path = path
        self.settings = settings
        self.state = self._load()

    @property
    def effective(self) -> int:
        if not self.settings.elastic.enabled:
            return self.settings.containers
        self.state.floor = self.settings.containers
        self.state.ceiling = max(self.settings.containers, self.settings.elastic.ceiling)
        self.state.effective = min(self.state.ceiling, max(self.state.floor, self.state.effective))
        return self.state.effective

    def scan_delay_seconds(self, files: list[Path], *, window_seconds: int | None = None) -> float:
        window = window_seconds or self.settings.elastic.window_seconds
        cutoff = time.time() - window
        total_ms = 0
        for path in files:
            try:
                if path.stat().st_mtime < cutoff:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")[-512 * 1024 :]
            except OSError:
                continue
            total_ms += sum(int(match.group(1)) for match in RETRY_RE.finditer(text))
        return total_ms / 1000

    async def adjust(
        self,
        *,
        stall_seconds: float,
        full: bool,
        waiting: bool,
        severe: bool = False,
        now: float | None = None,
    ) -> str:
        if not self.settings.elastic.enabled:
            return ""
        current = now or time.monotonic()
        window = max(1, self.settings.elastic.window_seconds)
        stall = stall_seconds / (max(1, self.effective) * window)
        threshold = self.settings.elastic.stall_percent / 100
        action = ""
        if (stall > threshold or severe) and current - self.state.last_down_at >= 180:
            if self.state.effective > self.state.floor:
                self.state.effective -= 1
                self.state.last_down_at = current
                action = "down"
            else:
                action = "floor"
        elif (
            full
            and waiting
            and stall < threshold / 2
            and current - self.state.last_down_at > self.settings.elastic.cooldown_seconds
            and current - self.state.last_up_at >= self.settings.elastic.step_up_seconds
            and self.state.effective < self.state.ceiling
        ):
            self.state.effective += 1
            self.state.last_up_at = current
            action = "up"
        self._remember()
        self._save()
        return action

    def note_stable(self, *, full: bool, pressure: bool, now: float | None = None) -> None:
        current = now or time.monotonic()
        if not full or pressure:
            self.state.last_stable_at = current
            return
        if current - self.state.last_stable_at < self.settings.elastic.stable_seconds:
            return
        hour = datetime.now().astimezone().strftime("%H")
        self.state.hourly_memory[hour] = self.state.effective
        self.state.last_stable_at = current
        self._save()

    def on_manual_floor(self) -> None:
        self.state.floor = self.settings.containers
        self.state.effective = self.state.floor
        self.state.last_up_at = time.monotonic()
        self._save()

    def _remember(self) -> None:
        self.state.hourly_memory = dict(list(self.state.hourly_memory.items())[-168:])

    def _save(self) -> None:
        write_json(self.path, self.state.view())

    def _load(self) -> ElasticState:
        data = read_json(self.path, {})
        hour = datetime.now().astimezone().strftime("%H")
        memory = data.get("hourly_memory") if isinstance(data, dict) else {}
        remembered = int(memory.get(hour) or self.settings.containers) if isinstance(memory, dict) else self.settings.containers
        return ElasticState(
            effective=min(8, max(1, int(data.get("effective") or remembered))) if isinstance(data, dict) else self.settings.containers,
            floor=self.settings.containers,
            ceiling=max(self.settings.containers, self.settings.elastic.ceiling),
            last_down_at=float(data.get("last_down_at") or 0) if isinstance(data, dict) else 0,
            last_up_at=float(data.get("last_up_at") or 0) if isinstance(data, dict) else 0,
            last_stable_at=float(data.get("last_stable_at") or 0) if isinstance(data, dict) else 0,
            hourly_memory={str(key): int(value) for key, value in memory.items()} if isinstance(memory, dict) else {},
        )
