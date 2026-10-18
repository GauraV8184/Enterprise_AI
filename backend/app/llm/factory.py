"""Factory that builds a chat model from settings.

Why a factory instead of constructing ``ChatOpenAI`` inline? Two reasons:

* **Provider swap without redesign.** All provider-specific knowledge lives in
  :func:`build_chat_model`. Adding Azure/Bedrock/Anthropic later is a new branch
  here; nothing else in the app changes.
* **Single validation point.** If the provider is not configured we raise
  :class:`LlmNotConfiguredError` at startup rather than failing on a user's first
  request with a confusing stack trace.

Field names are version-sensitive - ``langchain-openai`` uses ``model_name`` and
``openai_api_base``, not ``model``/``base_url``. They were verified against
langchain-openai 1.7.2.

Both supported providers speak the same OpenAI-compatible wire protocol, so they
share one implementation: ``ollama`` simply supplies a default local base URL and
a placeholder key.
"""

from __future__ import annotations

from typing import Any

from langchain_core.embeddings import Embeddings
from langchain_core.language_models import BaseChatModel

from app.core.config import Settings
from app.core.logging import get_logger, safe_extra
from app.llm.errors import LlmNotConfiguredError

logger = get_logger(__name__)


def build_chat_model(settings: Settings) -> BaseChatModel:
    """Build a provider client from validated settings.

    Raises:
        LlmNotConfiguredError: the selected provider has no usable credentials.
    """
    if settings.llm_provider == "openai":
        return _build_openai(settings)
    if settings.llm_provider == "ollama":
        return _build_ollama(settings)

    # Defensive: ``llm_provider`` is validated at startup, so this is unreachable
    # unless a new provider value is added without a corresponding branch.
    raise LlmNotConfiguredError(f"Unsupported LLM provider: {settings.llm_provider!r}")


def _build_openai(settings: Settings) -> BaseChatModel:
    if settings.openai_api_key is None:
        raise LlmNotConfiguredError()
    return _build_openai_compatible(
        settings,
        base_url=settings.effective_base_url,
        api_key=settings.effective_api_key,
    )


def _build_ollama(settings: Settings) -> BaseChatModel:
    """Build a client for a local Ollama server.

    Ollama ignores the API key, but the OpenAI client requires a non-empty
    value, so a placeholder is used when the user has not set one.
    """
    return _build_openai_compatible(
        settings,
        base_url=settings.effective_base_url,
        api_key=settings.effective_api_key,
    )


def _build_openai_compatible(
    settings: Settings,
    *,
    base_url: str | None,
    api_key: str | None,
) -> BaseChatModel:
    """Construct the OpenAI-compatible client shared by all providers."""
    from langchain_openai import ChatOpenAI

    kwargs: dict[str, Any] = {
        "model_name": settings.openai_model,
        # Passing a plain string is fine; pydantic coerces it to SecretStr, which
        # keeps the value out of repr() and tracebacks.
        "openai_api_key": api_key,
        "temperature": settings.llm_temperature,
        "request_timeout": settings.llm_timeout_seconds,
        "max_retries": settings.llm_max_retries,
    }
    if base_url:
        kwargs["openai_api_base"] = base_url
    if settings.llm_max_tokens is not None:
        kwargs["max_tokens"] = settings.llm_max_tokens

    logger.info(
        "llm_client_built",
        extra=safe_extra(
            provider=settings.llm_provider,
            model=settings.openai_model,
            base_url=base_url or "(provider default: api.openai.com)",
            timeout_seconds=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
        ),
    )
    return ChatOpenAI(**kwargs)


def build_embeddings(settings: Settings) -> Embeddings:
    """Build the embedding model from validated settings.

    Embeddings are configured separately from the chat model, so you can run a
    local embedding model alongside a hosted chat model (or vice versa).

    Raises:
        LlmNotConfiguredError: the selected embedding provider has no credentials.
    """
    if settings.embedding_provider == "ollama":
        return _build_openai_compatible_embeddings(
            settings,
            base_url=settings.effective_embedding_base_url,
            api_key=settings.effective_embedding_api_key,
        )
    if settings.embedding_provider == "openai":
        if settings.openai_api_key is None:
            raise LlmNotConfiguredError(
                "Embeddings are set to the openai provider but OPENAI_API_KEY is empty. "
                "Set a key, or use EMBEDDING_PROVIDER=ollama."
            )
        return _build_openai_compatible_embeddings(
            settings,
            base_url=settings.effective_embedding_base_url,
            api_key=settings.effective_embedding_api_key,
        )

    raise LlmNotConfiguredError(f"Unsupported embedding provider: {settings.embedding_provider!r}")


def _build_openai_compatible_embeddings(
    settings: Settings,
    *,
    base_url: str | None,
    api_key: str | None,
) -> Embeddings:
    """Construct the OpenAI-compatible embedding client.

    Note the field is ``model`` here (not ``model_name`` as on ChatOpenAI) and
    the endpoint is still ``openai_api_base``. Verified against
    langchain-openai 1.7.2.
    """
    from langchain_openai import OpenAIEmbeddings

    kwargs: dict[str, Any] = {
        "model": settings.embedding_model,
        "openai_api_key": api_key,
        "request_timeout": settings.embedding_timeout_seconds,
        "chunk_size": settings.embedding_batch_size,
        # Local servers do not implement the tokeniser-based length check, and
        # it would reject non-OpenAI models.
        "check_embedding_ctx_length": False,
    }
    if base_url:
        kwargs["openai_api_base"] = base_url

    logger.info(
        "embeddings_client_built",
        extra=safe_extra(
            provider=settings.embedding_provider,
            model=settings.embedding_model,
            base_url=base_url or "(provider default: api.openai.com)",
            batch_size=settings.embedding_batch_size,
        ),
    )
    return OpenAIEmbeddings(**kwargs)
