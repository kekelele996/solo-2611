"""FastAPI application entry point."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from solo_auto.api import (
    audit,
    autopilot,
    batches,
    events,
    evidence,
    feedback_export,
    intake,
    purge,
    recording,
    system,
    tasks,
    trash,
    usage,
)
from solo_auto.context import AppContext
from solo_auto.errors import ApiError

LOGGER = logging.getLogger(__name__)


class SecurityHeadersMiddleware:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                additions = {
                    b"x-content-type-options": b"nosniff",
                    b"x-frame-options": b"DENY",
                    b"referrer-policy": b"same-origin",
                    b"content-security-policy": (
                        b"default-src 'self'; script-src 'self' https://cdn.jsdelivr.net; "
                        b"style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
                        b"img-src 'self' data:; connect-src 'self'; font-src 'self' data:; frame-ancestors 'none'"
                    ),
                }
                existing = {key.lower() for key, _ in headers}
                headers.extend((key, value) for key, value in additions.items() if key not in existing)
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)


def create_app(home: Path | None = None, *, auto_start: bool = True) -> FastAPI:
    context = AppContext(home, auto_start=auto_start)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await context.startup()
        try:
            yield
        finally:
            await context.shutdown()

    app = FastAPI(
        title="solo-auto",
        version="0.2.0",
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )
    app.state.context = context
    app.add_middleware(SecurityHeadersMiddleware)

    app.include_router(system.router)
    app.include_router(purge.router)
    app.include_router(tasks.router)
    app.include_router(evidence.router)
    app.include_router(recording.router)
    app.include_router(feedback_export.router)
    app.include_router(autopilot.router)
    app.include_router(audit.router)
    app.include_router(batches.router)
    app.include_router(intake.router)
    app.include_router(trash.router)
    app.include_router(usage.router)
    app.include_router(events.router)

    @app.exception_handler(ApiError)
    async def api_error_handler(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=jsonable_encoder(
                {
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "detail": exc.detail,
                }
                }
            ),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content=jsonable_encoder(
                {
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": "请求参数校验失败",
                    "detail": exc.errors(),
                }
                }
            ),
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
        LOGGER.exception("unhandled API error on %s", request.url.path)
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": str(exc) or "未预期错误",
                    "detail": {"path": request.url.path},
                }
            },
        )

    dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    if dist.is_dir():
        assets = dist / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=assets), name="assets")

        @app.get("/{full_path:path}", include_in_schema=False, response_model=None)
        async def spa_fallback(full_path: str) -> FileResponse | JSONResponse:
            if full_path.startswith("api/"):
                return JSONResponse(status_code=404, content={"error": {"code": "NOT_FOUND", "message": "接口不存在"}})
            candidate = (dist / full_path).resolve()
            if full_path and candidate.is_file() and candidate.is_relative_to(dist.resolve()):
                return FileResponse(candidate)
            return FileResponse(dist / "index.html", headers={"Cache-Control": "no-cache"})

    else:

        @app.get("/", include_in_schema=False)
        async def no_frontend() -> PlainTextResponse:
            return PlainTextResponse(
                "solo-auto API 已启动。请先在 frontend/ 运行 npm install && npm run build。",
                status_code=200,
            )

    return app


app = create_app()
