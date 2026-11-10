import asyncio

import pytest

from solo_auto.api.events import SSEStreamingResponse
from solo_auto.events.bus import EventBus


@pytest.mark.asyncio
async def test_event_bus_replay_and_queue() -> None:
    bus = EventBus()
    queue = bus.subscribe()
    first = bus.publish("task.updated", {"task_id": "a"})
    second = bus.publish("queue.updated", {"limit": 4})
    assert first.seq + 1 == second.seq
    received = await asyncio.wait_for(queue.get(), timeout=1)
    assert received.event == "task.updated"
    assert [event.seq for event in bus.replay(first.seq)] == [second.seq]
    bus.unsubscribe(queue)


@pytest.mark.asyncio
async def test_sse_response_sends_without_disconnect_listener() -> None:
    async def body():
        yield "event: ping\n\n"

    response = SSEStreamingResponse(body(), {"Cache-Control": "no-cache"})
    messages: list[dict[str, object]] = []

    async def send(message: dict[str, object]) -> None:
        messages.append(message)

    async def receive() -> dict[str, object]:
        raise AssertionError("SSE 不应注册独立 disconnect receive 监听")

    await response({"type": "http"}, receive, send)
    assert messages[0]["type"] == "http.response.start"
    assert all(key.lower() != b"content-length" for key, _ in messages[0]["headers"])
    assert messages[1]["body"] == b"event: ping\n\n"
    assert messages[2]["more_body"] is False
