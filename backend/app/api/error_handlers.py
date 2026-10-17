"""Centralised exception handlers.

``AppError`` subclasses carry their own status code and client-safe message, so
one handler covers every expected failure (LLM errors today, tool and
authorization errors later). Anything unexpected becomes a generic 500 that
leaks no internals, while the full traceback goes to the logs.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.core.errors import AppError
from app.core.logging import get_logger, request_id_ctx, safe_extra

logger = get_logger(__name__)


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        request_id = request_id_ctx.get()
        # 5xx from our side is worth a warning; 4xx is expected client behaviour.
        log = logger.warning if exc.status_code < 500 else logger.error
        log(
            "app_error",
            extra=safe_extra(
                error_code=exc.code,
                status_code=exc.status_code,
                path=request.url.path,
                message=exc.message,
            ),
        )
        return JSONResponse(
            status_code=exc.status_code,
            content={**exc.to_payload(), "request_id": request_id},
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        request_id = request_id_ctx.get()
        logger.exception(
            "unhandled_exception",
            extra=safe_extra(path=request.url.path, error_type=type(exc).__name__),
        )
        return JSONResponse(
            status_code=500,
            content={
                "detail": "Internal server error.",
                "code": "internal_error",
                "request_id": request_id,
            },
        )
