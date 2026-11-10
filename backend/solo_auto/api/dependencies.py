"""Request-scoped access to the application services."""

from __future__ import annotations

from fastapi import Request

from solo_auto.context import AppContext


def context(request: Request) -> AppContext:
    return request.app.state.context
