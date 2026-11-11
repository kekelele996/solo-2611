"""Environment-gated fault injection shared by tests, drills, and soak runs."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class FaultSpec:
    fail: bool = False
    delay: float = 0.0
    value: Any = None


class FaultController:
    def __init__(self) -> None:
        self.enabled = os.environ.get("SOLO_AUTO_FAULTS") == "1"
        self._faults: dict[str, FaultSpec] = {}
        raw = os.environ.get("SOLO_AUTO_FAULT_MAP", "")
        if self.enabled and raw:
            try:
                values = json.loads(raw)
            except json.JSONDecodeError:
                values = {}
            if isinstance(values, dict):
                self._faults = {
                    str(key): FaultSpec(
                        fail=bool(value.get("fail")) if isinstance(value, dict) else bool(value),
                        delay=float(value.get("delay") or 0) if isinstance(value, dict) else 0.0,
                        value=value.get("value") if isinstance(value, dict) else None,
                    )
                    for key, value in values.items()
                }

    def set(self, name: str, *, fail: bool = False, delay: float = 0.0, value: Any = None) -> None:
        if self.enabled:
            self._faults[name] = FaultSpec(fail=fail, delay=delay, value=value)

    def clear(self) -> None:
        self._faults.clear()

    def get(self, name: str) -> FaultSpec:
        return self._faults.get(name, FaultSpec()) if self.enabled else FaultSpec()

    async def pause(self, name: str) -> None:
        spec = self.get(name)
        if spec.delay > 0:
            await asyncio.sleep(spec.delay)
