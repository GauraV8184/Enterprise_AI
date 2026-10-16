"""FastAPI application entry point.

Expose ``create_app()`` so tests can build isolated app instances, and a
module-level ``app`` for ``uvicorn app.main:app``.

Run locally (from the ``backend`` directory):
    uvicorn app.main:app --reload --port 8000
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.router import api_router
from app.core.config import Settings, get_settings
from app.core.logging import get_logger, request_id_ctx, setup_logging

logger = get_logger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build and configure a FastAPI application instance."""
    settings = settings or get_settings()
    setup_logging(settings)

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        debug=settings.debug,
        docs_url="/docs" if not settings.is_production else None,
        redoc_url="/redoc" if not settings.is_production else None,
        openapi_url="/openapi.json" if not settings.is_production else None,
    )
    # Settings are read per-request from app.state; see app/api/deps.py.
    app.state.settings = settings

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=[REQUEST_ID_HEADER],
    )

    _register_request_context(app)
    _register_exception_handlers(app)

    app.include_router(api_router, prefix=settings.api_prefix)
    # Also serve "/" so a bare visit to the host shows useful metadata.
    app.include_router(api_router)

    logger.info(
        "application_started",
        extra={"environment": settings.environment, "version": settings.app_version},
    )
    return app


def _register_request_context(app: FastAPI) -> None:
    """Attach a request id and log one line per request with its duration."""

    @app.middleware("http")
    async def request_context(request: Request, call_next: Callable[[Request], Awaitable[object]]):
        request_id = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex
        token = request_id_ctx.set(request_id)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            # Let the dedicated exception handlers shape the response; log here
            # so the failure is still correlated with the request id.
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            logger.exception(
                "request_failed",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "duration_ms": duration_ms,
                },
            )
            raise
        finally:
            request_id_ctx.reset(token)

        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        response.headers[REQUEST_ID_HEADER] = request_id
        logger.info(
            "request_completed",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "duration_ms": duration_ms,
            },
        )
        return response


def _register_exception_handlers(app: FastAPI) -> None:
    """Return consistent JSON errors instead of bare tracebacks."""

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        logger.exception("unhandled_exception", extra={"path": request.url.path})
        return JSONResponse(
            status_code=500,
            content={
                "detail": "Internal server error.",
                "request_id": request_id_ctx.get(),
            },
        )


app = create_app()
