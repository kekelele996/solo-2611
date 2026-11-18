"""Interrupt one identified turn on the local Codex app-server socket."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from websockets.asyncio.client import unix_connect
from websockets.exceptions import WebSocketException


async def interrupt_turn(thread_id: str, turn_id: str, *, socket: Path | None = None) -> str:
    socket = socket or Path(os.environ.get("SOLO_AUTO_DESKTOP_SOCKET", "~/.codex/app-server-control/app-server-control.sock")).expanduser()
    if not socket.is_socket():
        return "桌面控制连接不可用，保留活跃会话占用"
    try:
        async with asyncio.timeout(5), unix_connect(str(socket), close_timeout=1) as connection:
            async def request(number: int, method: str, params: dict[str, Any]) -> None:
                await connection.send(json.dumps({"id": number, "method": method, "params": params}))
                async for frame in connection:
                    reply = json.loads(frame)
                    if reply.get("id") == number:
                        if "result" not in reply:
                            raise ValueError("控制请求未获确认")
                        return
                raise ValueError("控制连接已关闭")

            await request(1, "initialize", {"clientInfo": {"name": "solo-auto", "version": "1"}})
            await connection.send(json.dumps({"method": "initialized"}))
            await request(2, "turn/interrupt", {"threadId": thread_id, "turnId": turn_id})
            return "已请求停止指定回合，等待轨迹确认"
    except (TimeoutError, OSError, ValueError, WebSocketException):
        return "停止回合未获确认，保留活跃会话占用"
