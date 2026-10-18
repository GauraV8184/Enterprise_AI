"""Shared FastAPI dependencies.

Settings, the chat model, the document store and the embedding model are built
once during startup and attached to ``app.state``. Tests replace them by setting
the attribute directly or via ``app.dependency_overrides``, so the suite never
touches the network or the developer's real database.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request
from langchain_core.embeddings import Embeddings
from langchain_core.language_models import BaseChatModel

from app.core.config import Settings
from app.database.repositories import DocumentRepository
from app.llm.errors import LlmNotConfiguredError


def get_app_settings(request: Request) -> Settings:
    """Return the ``Settings`` bound to the current application instance."""
    return request.app.state.settings


def get_chat_model(request: Request) -> BaseChatModel:
    """Return the app's chat model, or fail clearly if none is configured."""
    model = getattr(request.app.state, "chat_model", None)
    if model is None:
        raise LlmNotConfiguredError()
    return model


def get_repository(request: Request) -> DocumentRepository:
    """Return the document store for this application instance."""
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
RepositoryDep = Annotated[DocumentRepository, Depends(get_repository)]
EmbeddingsDep = Annotated[Embeddings, Depends(get_embeddings)]
