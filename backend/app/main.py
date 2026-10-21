"""FastAPI application entry point.

Expose ``create_app()`` so tests can build isolated app instances, and a
module-level ``app`` for ``uvicorn app.main:app``.

Run locally (from the ``backend`` directory):
    uvicorn app.main:app --reload --port 8000
"""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.api.error_handlers import register_error_handlers
from app.api.router import api_router
from app.core.config import Settings, get_settings
from app.core.logging import get_logger, request_id_ctx, safe_extra, setup_logging
from app.database.repositories import DocumentRepository
from app.llm.errors import LlmNotConfiguredError
from app.llm.factory import build_chat_model, build_embeddings
from app.security.supabase_auth import SupabaseTokenVerifier
from app.storage.local import LocalFileStore
from app.tools.web_search import build_web_search

logger = get_logger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def _request_id(request: Request) -> str:
    supplied = request.headers.get(REQUEST_ID_HEADER)
    if supplied and _VALID_REQUEST_ID.fullmatch(supplied):
        return supplied
    return uuid.uuid4().hex


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
    app.include_router(api_router)

    logger.info(
        "application_started",
        extra=safe_extra(environment=settings.environment, version=settings.app_version),
    )
    return app


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Build long-lived resources once at startup.

    Building clients here (rather than per request) means one HTTP connection
    pool, and it surfaces configuration problems in the startup logs. Note that
    no network call happens yet - a bad key is only discovered on first use,
    which ``scripts/check_supabase.py`` and ``scripts/check_llm.py`` exist to
    catch before that.

    Raises:
        ValueError: the configuration cannot work (see
            :meth:`~app.core.config.Settings.validate_supabase_expectations`).
            Refusing to start is better than serving requests that can only 503.
    """
    settings: Settings = app.state.settings

    settings.validate_supabase_expectations()

    app.state.supabase_http = None
    app.state.token_verifier = None

    if settings.use_supabase_auth:
        verifier = SupabaseTokenVerifier(settings)
        app.state.token_verifier = verifier
        logger.info("supabase_auth_ready", extra=safe_extra(issuer=verifier.issuer))

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

    if settings.use_supabase_store or settings.use_supabase_storage:
        app.state.supabase_http = httpx.Client(
            timeout=httpx.Timeout(30.0, connect=10.0),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )

    if settings.use_supabase_store:
        app.state.repository = None
        logger.info("document_store_ready", extra=safe_extra(backend="supabase"))
    else:
        app.state.repository = DocumentRepository(settings.database_path)

    if settings.use_supabase_storage:
        app.state.file_store = None
        logger.info(
            "file_storage_ready",
            extra=safe_extra(backend="supabase", bucket=settings.supabase_documents_bucket),
        )
    else:
        app.state.file_store = LocalFileStore(settings.uploads_dir)
        logger.info("file_storage_ready", extra=safe_extra(backend="local"))

    if settings.embeddings_configured:
        try:
            app.state.embeddings = build_embeddings(settings)
        except LlmNotConfiguredError as exc:
            app.state.embeddings = None
            logger.warning(
                "embeddings_not_configured",
                extra=safe_extra(provider=settings.embedding_provider, reason=exc.message),
            )
    else:
        app.state.embeddings = None
        logger.warning(
            "embeddings_not_configured",
            extra=safe_extra(
                provider=settings.embedding_provider,
                hint="Set EMBEDDING_PROVIDER/EMBEDDING_MODEL in .env",
            ),
        )

    app.state.web_search = build_web_search(settings)
    if app.state.web_search is None:
        logger.info(
            "web_search_not_configured",
            extra=safe_extra(
                hint="Set TAVILY_API_KEY in the repository-root .env file to enable the research route",
            ),
        )
    else:
        logger.info("web_search_ready", extra=safe_extra(provider="tavily"))

    yield

    if app.state.supabase_http is not None:
        app.state.supabase_http.close()
        app.state.supabase_http = None
    if app.state.web_search is not None:
        close = getattr(app.state.web_search, "close", None)
        if close is not None:
            close()
        app.state.web_search = None
    app.state.chat_model = None
    app.state.embeddings = None
    app.state.token_verifier = None
    app.state.file_store = None


def _register_request_context(app: FastAPI) -> None:
    """Attach a request id and log one line per request with its duration."""

    @app.middleware("http")
    async def request_context(request: Request, call_next: Callable[[Request], Awaitable[object]]):
        request_id = _request_id(request)
        token = request_id_ctx.set(request_id)
        started = time.perf_counter()
        try:
            response = await call_next(request)
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
        except Exception:
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


app = create_app()
