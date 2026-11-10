"""SSE event channel."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterable, AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, Header
from fastapi.responses import Response
from starlette.requests import ClientDisconnect

from solo_auto.api.dependencies import context
from solo_auto.context import AppContext

router = APIRouter(prefix="/api")


class SSEStreamingResponse(Response):
    """SSE response without Starlette's extra disconnect listener task."""

    media_type = "text/event-stream"

    def __init__(self, content: AsyncIterable[str], headers: dict[str, str]) -> None:
        super().__init__(content=None, media_type=self.media_type, headers=headers)
        self.body_iterator = content

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        del receive
        try:
            await send(
                {
                    "type": "http.response.start",
                    "status": self.status_code,
                    "headers": self.raw_headers,
                }
            )
            async for chunk in self.body_iterator:
                await send(
                    {
                        "type": "http.response.body",
                        "body": chunk.encode("utf-8") if isinstance(chunk, str) else chunk,
                        "more_body": True,
                    }
                )
            await send({"type": "http.response.body", "body": b"", "more_body": False})
        except (asyncio.CancelledError, OSError, ClientDisconnect):
            return


@router.get("/events/stream")
async def stream(
    ctx: AppContext = Depends(context),
    last_event_id: str = Header(default="0", alias="Last-Event-ID"),
) -> SSEStreamingResponse:
    try:
        after = int(last_event_id)
    except ValueError:
        after = 0
    queue = ctx.bus.subscribe()

    async def generator() -> AsyncIterator[str]:
        try:
            for event in ctx.bus.replay(after):
                yield event.sse()
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                    yield event.sse()
                except TimeoutError:
                    yield f": keepalive {ctx.bus.sequence}\n\n"
        except asyncio.CancelledError:
            return
        finally:
            ctx.bus.unsubscribe(queue)

    return SSEStreamingResponse(
        generator(),
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
