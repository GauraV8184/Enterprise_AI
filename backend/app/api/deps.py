"""Shared FastAPI dependencies.

Settings, the chat model, the embedding model and the SQLite store are built once
during startup and attached to ``app.state``. Tests replace them by setting the
attribute directly or via ``app.dependency_overrides``, so the suite never
touches the network or the developer's real database.

The Supabase store is the exception: it is built per request, because it carries
the caller's access token. See :func:`get_repository`.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request
from langchain_core.embeddings import Embeddings
from langchain_core.language_models import BaseChatModel

from app.core.config import Settings
from app.database.postgrest import PostgrestClient
from app.database.repositories import DocumentStore
from app.database.supabase_repository import SupabaseDocumentRepository
from app.llm.errors import LlmNotConfiguredError
from app.security.errors import NotAuthenticatedError
from app.security.identity import (
    AUTHORIZATION_HEADER,
    CurrentUserDep,
    extract_bearer_token,
)


def get_app_settings(request: Request) -> Settings:
    """Return the ``Settings`` bound to the current application instance."""
    return request.app.state.settings


def get_chat_model(request: Request) -> BaseChatModel:
    """Return the app's chat model, or fail clearly if none is configured."""
    model = getattr(request.app.state, "chat_model", None)
    if model is None:
        raise LlmNotConfiguredError()
    return model


def get_repository(request: Request, user: CurrentUserDep) -> DocumentStore:
    """Return the document store for this request.

    ``user`` is not used in the body. It is a dependency so that identity is
    resolved - and, in Supabase mode, verified - before any query is built. A
    store call that ran first would have to guess whose token to forward.

    The two backends differ in lifetime for a reason. SQLite scopes every query by
    ``owner_id``, so one process-wide object is safe. The Supabase store attaches
    the caller's access token to each request; sharing it would let concurrent
    requests act as each other. A per-request wrapper around a pooled HTTP client
    costs almost nothing.
    """
    settings: Settings = request.app.state.settings

    if settings.use_supabase_store:
        http_client = getattr(request.app.state, "supabase_http", None)
        if http_client is None:
            raise RuntimeError("The Supabase HTTP client was not initialised at startup.")
        if settings.supabase_anon_key is None:
            raise RuntimeError("SUPABASE_ANON_KEY is missing; refusing to query without it.")

        token = extract_bearer_token(request.headers.get(AUTHORIZATION_HEADER))
        if token is None:
            raise NotAuthenticatedError()

        return SupabaseDocumentRepository(
            PostgrestClient(
                base_url=settings.supabase_service_url("rest"),
                api_key=settings.supabase_anon_key.get_secret_value(),
                access_token=token,
                client=http_client,
            )
        )

    repository = getattr(request.app.state, "repository", None)
    if repository is None:
        # Only reachable if the lifespan was skipped (e.g. a raw ASGI call).
        raise RuntimeError("The document repository was not initialised at startup.")
    return repository


def get_embeddings(request: Request) -> Embeddings:
    """Return the app's embedding model, or fail clearly if none is configured."""
    embeddings = getattr(request.app.state, "embeddings", None)
    if embeddings is None:
        raise LlmNotConfiguredError(
            "No embedding model is configured. Set EMBEDDING_PROVIDER and EMBEDDING_MODEL "
            "in .env, or run LLM_PROVIDER=ollama with `ollama pull nomic-embed-text`."
        )
    return embeddings


# Reusable annotated dependencies. Using ``Annotated`` (the FastAPI-recommended
# style) keeps ``Depends`` out of function defaults, which satisfies lint rules.
SettingsDep = Annotated[Settings, Depends(get_app_settings)]
ChatModelDep = Annotated[BaseChatModel, Depends(get_chat_model)]
RepositoryDep = Annotated[DocumentStore, Depends(get_repository)]
EmbeddingsDep = Annotated[Embeddings, Depends(get_embeddings)]
