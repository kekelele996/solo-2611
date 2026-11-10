"""In-memory SSE event bus backed by persisted task events."""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(slots=True)
class BusEvent:
    seq: int
    event: str
    data: dict[str, Any]

    def sse(self) -> str:
        return f"id: {self.seq}\nevent: {self.event}\ndata: {json.dumps(self.data, ensure_ascii=False)}\n\n"


class EventBus:
    def __init__(self) -> None:
        self._seq = 0
        self._queues: set[asyncio.Queue[BusEvent]] = set()
        self._history: list[BusEvent] = []

    @property
    def sequence(self) -> int:
        return self._seq

    def subscribe(self) -> asyncio.Queue[BusEvent]:
        queue: asyncio.Queue[BusEvent] = asyncio.Queue(maxsize=512)
        self._queues.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[BusEvent]) -> None:
        self._queues.discard(queue)

    def publish(self, event: str, data: dict[str, Any]) -> BusEvent:
        self._seq += 1
        payload = dict(data)
        payload.setdefault("at", datetime.now().astimezone().replace(microsecond=0).isoformat())
        item = BusEvent(self._seq, event, payload)
        self._history.append(item)
        self._history = self._history[-500:]
        for queue in tuple(self._queues):
            if queue.full():
                with suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            queue.put_nowait(item)
        return item

    def replay(self, after_id: int) -> list[BusEvent]:
        return [event for event in self._history if event.seq > after_id]
