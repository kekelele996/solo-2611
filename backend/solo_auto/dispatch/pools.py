"""Resource pools and container reservations."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from solo_auto.dispatch.facts import FactsSnapshot


@dataclass(slots=True)
class PoolState:
    name: str
    used: int = 0
    limit: int = 0
    reserved: dict[str, float] = field(default_factory=dict)
    observed: set[str] = field(default_factory=set)

    @property
    def available(self) -> int:
        return max(0, self.limit - self.used)

    @property
    def draining(self) -> bool:
        return self.used > self.limit

    def view(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "used": self.used,
            "limit": self.limit,
            "available": self.available,
            "draining": self.draining,
        }


class PoolBook:
    def __init__(self) -> None:
        self.pools: dict[str, PoolState] = {
            name: PoolState(name)
            for name in ("containers", "sessions", "authoring", "delivering", "recording", "exporting")
        }
        self._reserved_containers: dict[str, float] = {}

    def configure(
        self,
        *,
        containers: int,
        sessions: int,
        authoring: int,
        delivering: int,
        recording: int,
    ) -> None:
        self.pools["containers"].limit = containers
        self.pools["sessions"].limit = sessions
        self.pools["authoring"].limit = authoring
        self.pools["delivering"].limit = delivering
        self.pools["recording"].limit = recording
        self.pools["exporting"].limit = 1

    def occupy_from_facts(self, facts: FactsSnapshot) -> None:
        managed = set(facts.docker.containers)
        reservation_count = len([task for task in self._reserved_containers if task not in managed])
        self.pools["containers"].used = len(managed) + reservation_count

    def reserve_container(self, task_id: str) -> None:
        self._reserved_containers[task_id] = time.monotonic()

    def release_container(self, task_id: str) -> None:
        self._reserved_containers.pop(task_id, None)

    def container_waiting(self) -> int:
        return len(self._reserved_containers)

    def acquire(self, pool: str, key: str) -> bool:
        state = self.pools[pool]
        if key in state.reserved:
            return True
        if state.used >= state.limit:
            return False
        state.reserved[key] = time.monotonic()
        state.used = len(set(state.reserved) | state.observed)
        return True

    def release(self, pool: str, key: str) -> None:
        state = self.pools[pool]
        state.reserved.pop(key, None)
        state.used = len(set(state.reserved) | state.observed)

    def observe(self, pool: str, keys: set[str]) -> None:
        state = self.pools[pool]
        state.observed = set(keys)
        state.used = len(set(state.reserved) | state.observed)

    def release_task(self, task_id: str) -> None:
        self.release_container(task_id)
        for pool in ("sessions", "authoring", "delivering", "recording", "exporting"):
            key = f"task:{task_id}"
            self.release(pool, key)

    def snapshot(self) -> dict[str, dict[str, Any]]:
        return {name: pool.view() for name, pool in self.pools.items()}

    def reconciliation_needed(self, facts: FactsSnapshot) -> dict[str, Any]:
        live = set(facts.docker.containers)
        reservations = set(self._reserved_containers)
        return {
            "live": sorted(live),
            "reserved": sorted(reservations),
            "known_union": sorted(live | reservations),
            "foreign": sorted(live - reservations),
        }
