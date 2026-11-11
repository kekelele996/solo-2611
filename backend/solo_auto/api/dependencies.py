"""Request-scoped access to the application services."""

from __future__ import annotations

from typing import cast

from fastapi import Request

from solo_auto.context import AppContext


def context(request: Request) -> AppContext:
    return cast(AppContext, request.app.state.context)
