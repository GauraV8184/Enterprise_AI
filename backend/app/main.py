"""FastAPI application entry point.

Expose ``create_app()`` so tests can build isolated app instances, and a
module-level ``app`` for ``uvicorn app.main:app``.

Run locally (from the ``backend`` directory):
    uvicorn app.main:app --reload --port 8000
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.api.error_handlers import register_error_handlers
from app.api.router import api_router
from app.core.config import Settings, get_settings
from app.core.logging import get_logger, request_id_ctx, safe_extra, setup_logging
from app.llm.errors import LlmNotConfiguredError
from app.llm.factory import build_chat_model

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
        lifespan=_lifespan,
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
    register_error_handlers(app)

    app.include_router(api_router, prefix=settings.api_prefix)
    # Also serve "/" so a bare visit to the host shows useful metadata.
    app.include_router(api_router)

    logger.info(
        "application_started",
        extra=safe_extra(environment=settings.environment, version=settings.app_version),
    )
    return app


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Build long-lived resources once at startup.

    Building the client here (rather than per request) means one HTTP connection
    pool, and it surfaces configuration problems in the startup logs. Note that
    no network call happens yet - a bad API key is only discovered on first use,
    which ``scripts/check_llm.py`` is designed to catch early.
    """
    settings: Settings = app.state.settings
    if settings.llm_configured:
        try:
            app.state.chat_model = build_chat_model(settings)
        except LlmNotConfiguredError:
            app.state.chat_model = None
            logger.warning("llm_not_configured", extra=safe_extra(provider=settings.llm_provider))
    else:
        app.state.chat_model = None
        logger.warning(
            "llm_not_configured",
            extra=safe_extra(
                provider=settings.llm_provider,
                hint="Set OPENAI_API_KEY in the repository-root .env file",
            ),
        )

    yield
    # Nothing to tear down yet; database/vector-store clients are released here
    # once Phases 3-4 introduce them.
    app.state.chat_model = None


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
            # Expected failures are handled by register_error_handlers; log here
            # so every failure is still correlated with the request id.
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            logger.exception(
                "request_failed",
                extra=safe_extra(
                    method=request.method,
                    path=request.url.path,
                    duration_ms=duration_ms,
                ),
            )
            raise
        finally:
            request_id_ctx.reset(token)

        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        response.headers[REQUEST_ID_HEADER] = request_id
        logger.info(
            "request_completed",
            extra=safe_extra(
                method=request.method,
                path=request.url.path,
                status_code=response.status_code,
                duration_ms=duration_ms,
            ),
        )
        return response


app = create_app()
