"""FastAPI app factory — thin transport over the runtime (plan §5).

`create_app(settings)` builds the AppContainer on startup (no globals),
wires the routers under `/v1`, and maps every failure onto the error
envelope."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from jarvis import __version__
from jarvis.api.deps import AppContainer
from jarvis.api.errors import ApiError, envelope
from jarvis.api.routes.agents import router as agents_router
from jarvis.api.routes.conversations import router as conversations_router
from jarvis.api.routes.executions import router as executions_router
from jarvis.config import Settings

logger = logging.getLogger("jarvis.api")


def create_app(settings: Settings | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        container = AppContainer.from_settings(app.state.settings)
        app.state.container = container
        try:
            yield
        finally:
            await container.aclose()

    app = FastAPI(title="JARVIS", version=__version__, lifespan=lifespan)
    app.state.settings = settings or Settings()

    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=envelope(exc.kind, exc.message, exc.details),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        details = [
            {"loc": list(e.get("loc", [])), "msg": e.get("msg"), "type": e.get("type")}
            for e in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content=envelope("validation", "request validation failed", {"errors": details}),
        )

    @app.exception_handler(Exception)
    async def _internal_error(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error")
        return JSONResponse(
            status_code=500, content=envelope("internal", "internal server error")
        )

    app.include_router(agents_router, prefix="/v1")
    app.include_router(executions_router, prefix="/v1")
    app.include_router(conversations_router, prefix="/v1")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


__all__ = ["create_app"]
